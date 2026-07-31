"""
harness.py -- end-to-end simulator verification that cannot pass vacuously
(Milestone R6.2A).

--------------------------------------------------------------------------------
WHY THIS EXISTS
--------------------------------------------------------------------------------
Compiling, assembling and running a program proves nothing on its own. A run is
only evidence if a REFERENCE says what the answer should be and something
actually compares against it. Three separate ways of losing that were found in
the existing flow, each of which produced a green-looking run:

  1. The native reference build silently failed.  `try_golden_verify` compiles
     the test with gcc against `golden_stubs.h` to get independent ground truth.
     Until 2026-07-31 that header declared only `vu8_t`, so ANY test using
     another packed marker (`vi8_t`, `vi16_t`, `vu16_t`, `vi32_t`, `vu32_t`,
     `vf32_t`) failed to compile natively. The failure is caught and reported --
     and then the flow falls back to a placeholder .result and carries on.
  2. The placeholder .result is EMPTY.  `mcode_run -r empty` has nothing to
     compare, so it prints no PostCondition lines and reports no error. The run
     looks perfect. It checked nothing.
  3. `mcode_run` exits 0 even when a PostCondition FAILS.  Verified directly:
     corrupting one expected word makes it print
        Error: PostCondition Mem[0x83] = 0x18, expected 0xff
     and still exit 0. Any harness that trusts `$?` reports success on wrong
     output.

So this harness never infers success from the absence of a complaint. It
requires POSITIVE evidence at every stage, and the number of comparisons that
actually happened must equal the number the test declares.

--------------------------------------------------------------------------------
THE SIX CHECKS  (all must hold; any failure is a hard FAIL)
--------------------------------------------------------------------------------
    1 native      gcc compiled the test against golden_stubs.h and ran it
    2 golden      a real .result was written, with exactly n_results entries,
                  and it is not the placeholder
    3 build       mcode_align and mcode_assemble reported no error
    4 executed    mcode_run ran the program to a halt and reported its tick count
    5 compared    mcode_run emitted exactly n_results PostCondition lines
    6 clean       mcode_run emitted zero `Error:` lines

Check 5 is the one that closes the vacuity hole: a missing reference produces
ZERO comparisons, which is now a failure rather than a silent pass.

--------------------------------------------------------------------------------
TOOLCHAIN INVOCATION
--------------------------------------------------------------------------------
This module DOES invoke the external assembler and simulator, which the rest of
the repository deliberately never does (every other .py only generates text). It
is confined to `verification/`, it is never imported by the compiler or by any
unit test, and it must be run explicitly. `APARA_TOOLS` overrides the toolchain
directory. `mcode_run` is invoked WITHOUT `-v`: the verbose trace of a loop
kernel reaches gigabytes.
"""

import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO

_HERE = os.path.dirname(os.path.abspath(__file__))
_C = os.path.dirname(_HERE)
sys.path.insert(0, _C)

DEFAULT_TOOLS = ('/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/'
                 'AjitHpcAccel/engine_isp/assembler/bin')

# mcode_align prints per-instruction parse chatter to stderr on every run; only
# these markers indicate a real failure.
_ERR_RE = re.compile(r'\b(Error|ERROR|error:|Fatal|failed)\b')
_POST_RE = re.compile(r'^Info:\s*PostCondition\b', re.M)
_POSTERR_RE = re.compile(r'^Error:.*$', re.M)
_TICKS_RE = re.compile(r'Stopped after (\d+) ticks')
_NONNULL_RE = re.compile(r'number of non-null instructions executed = (\d+)')
_NULL_RE = re.compile(r'number of null instructions executed = (\d+)')


def tools_dir():
    d = os.environ.get('APARA_TOOLS', DEFAULT_TOOLS)
    return d


def toolchain_available():
    d = tools_dir()
    return all(os.path.isfile(os.path.join(d, t)) and
               os.access(os.path.join(d, t), os.X_OK)
               for t in ('mcode_align', 'mcode_assemble', 'mcode_run'))


class Verdict:
    """The outcome of one verified program. `ok` is True only when every one of
    the six checks passed; `stage` names the first that did not."""

    STAGES = ('native', 'golden', 'build', 'executed', 'compared', 'clean')

    def __init__(self, name):
        self.name = name
        self.ok = False
        self.stage = None
        self.reason = ''
        self.checks = {s: False for s in self.STAGES}
        # evidence
        self.golden_values = 0        # PostConditions the reference declares
        self.postconditions = 0       # comparisons the simulator actually made
        self.errors = []              # simulator Error: lines
        self.ticks = None
        self.non_null = None
        self.null = None
        self.static_bundles = None
        self.static_instructions = None
        self.vectorized = None

    def fail(self, stage, reason):
        self.ok = False
        self.stage = stage
        self.reason = reason
        return self

    def passed(self):
        self.ok = True
        self.stage = None
        return self

    @property
    def issue_slots(self):
        if self.non_null is None or self.null is None:
            return None
        return self.non_null + self.null

    @property
    def dynamic_ipb(self):
        """Measured dynamic instructions per BUNDLE: non-null instructions
        divided by the bundles actually executed (one per tick)."""
        if self.non_null is None or not self.ticks:
            return None
        return self.non_null / self.ticks

    @property
    def dynamic_occupancy(self):
        """Measured fraction of issued slots that carried a real instruction."""
        s = self.issue_slots
        return (self.non_null / s) if s else None

    def __repr__(self):
        if self.ok:
            return (f"PASS {self.name:28s} {self.postconditions:2d} checks  "
                    f"{self.ticks} ticks  ipb={self.dynamic_ipb:.3f}")
        return f"FAIL {self.name:28s} [{self.stage}] {self.reason}"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def compile_program(name, source, workdir, vectorize=True, timeout=600):
    """Compile one C source with the production compiler.

    Returns (ok, info) where info carries the emitted paths, the captured
    compiler output, and the static bundle/instruction counts. Golden reference
    generation is part of `compile_c_to_mcode`, so its success is decided here
    by inspecting what it actually wrote -- never by its exit status."""
    os.makedirs(workdir, exist_ok=True)
    c_path = os.path.join(workdir, f'{name}.c')
    with open(c_path, 'w') as f:
        f.write(source)
    out_mcode = os.path.join(workdir, f'{name}.mcode')

    env_backup = os.environ.get('APARA_NO_VECTORIZE')
    if not vectorize:
        os.environ['APARA_NO_VECTORIZE'] = '1'
    buf = StringIO()
    try:
        from compiler import compile_c_to_mcode
        with redirect_stdout(buf):
            compile_c_to_mcode(c_path, out_mcode)
    except SystemExit as e:
        return False, {'output': buf.getvalue(), 'error': f'compiler exit {e}'}
    except Exception as e:
        return False, {'output': buf.getvalue(),
                       'error': f'{type(e).__name__}: {e}'}
    finally:
        if not vectorize:
            if env_backup is None:
                os.environ.pop('APARA_NO_VECTORIZE', None)
            else:
                os.environ['APARA_NO_VECTORIZE'] = env_backup

    text = buf.getvalue()
    m = re.search(r'bundles:\s*(\d+)\s*→\s*(\d+)', text)
    return True, {
        'output': text,
        'c': c_path,
        'mcode': out_mcode,
        'result': os.path.join(workdir, f'{name}.result'),
        'datamap': os.path.join(workdir, 'data.map'),
        'static_instructions': int(m.group(1)) if m else None,
        'static_bundles': int(m.group(2)) if m else None,
        'golden_line': 'golden    →' in text,
        'golden_failed': '[GOLDEN VERIFY]' in text,
    }


def _count_golden(path):
    """PostCondition lines a reference file declares. A missing or empty file --
    the placeholder the old flow wrote -- counts zero, which fails check 2."""
    if not os.path.isfile(path):
        return 0
    with open(path) as f:
        return sum(1 for ln in f if ln.strip().startswith('0 '))


def verify_program(name, source, n_results, workdir, vectorize=True,
                   corrupt=None):
    """Compile, assemble, simulate and VERIFY one program.

    `n_results` is how many PostCondition comparisons the test declares -- the
    number the harness demands actually happen, supplied by the test rather than
    read back out of the tool output.

    `corrupt` is the negative control: an index into the reference file whose
    expected value is flipped before the run, to demonstrate that a wrong result
    cannot pass."""
    v = Verdict(name)
    # file-safe stem: a name with spaces would still work through subprocess
    # argument lists, but it makes the emitted artifacts awkward to inspect by
    # hand, which is exactly what someone does when a check fails.
    stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', name)
    ok, info = compile_program(stem, source, workdir, vectorize=vectorize)
    if not ok:
        return v.fail('native', info.get('error', 'compilation failed'))
    v.static_bundles = info['static_bundles']
    v.static_instructions = info['static_instructions']

    # ── check 1: the NATIVE reference build succeeded ─────────────────────────
    # `try_golden_verify` prints "[GOLDEN VERIFY] ..." and returns False on any
    # native failure, then compile_c_to_mcode writes a placeholder instead. That
    # fallback is exactly the weakness R6.2A closes, so it is a hard failure here.
    if info['golden_failed'] or not info['golden_line']:
        return v.fail('native',
                      'native gcc reference build did not produce a golden '
                      'result (placeholder fallback taken)')
    v.checks['native'] = True

    # ── check 2: a REAL reference exists, with the declared size ──────────────
    got = _count_golden(info['result'])
    v.golden_values = got
    if got == 0:
        return v.fail('golden', 'reference file is empty (placeholder)')
    if got != n_results:
        return v.fail('golden',
                      f'reference declares {got} values, test declares '
                      f'{n_results}')
    v.checks['golden'] = True

    if corrupt is not None:
        with open(info['result']) as f:
            lines = f.read().splitlines()
        parts = lines[corrupt].split()
        parts[-1] = '0x%016x' % ((int(parts[-1], 16) ^ 0xFF) & ((1 << 64) - 1))
        lines[corrupt] = ' '.join(parts)
        with open(info['result'], 'w') as f:
            f.write('\n'.join(lines) + '\n')

    # ── check 3: assemble ─────────────────────────────────────────────────────
    B = tools_dir()
    aligned = os.path.join(workdir, f'{stem}.aligned.mcode')
    obj = os.path.join(workdir, f'{stem}.obj')
    r = _run([os.path.join(B, 'mcode_align'), info['mcode']])
    if r.returncode != 0 or _ERR_RE.search(r.stderr or ''):
        return v.fail('build', f'mcode_align: {(r.stderr or "").strip()[:200]}')
    with open(aligned, 'w') as f:
        f.write(r.stdout)
    r = _run([os.path.join(B, 'mcode_assemble'), aligned])
    if r.returncode != 0 or _ERR_RE.search(r.stderr or ''):
        return v.fail('build', f'mcode_assemble: {(r.stderr or "").strip()[:200]}')
    with open(obj, 'w') as f:
        f.write(r.stdout)
    v.checks['build'] = True

    # ── check 4: the simulator RAN (no -v: the trace reaches gigabytes) ───────
    r = _run([os.path.join(B, 'mcode_run'), '-p', '0x0', '-i', obj,
              '-d', info['datamap'], '-r', info['result']], timeout=600)
    out = (r.stdout or '') + (r.stderr or '')
    mt = _TICKS_RE.search(out)
    if mt is None:
        return v.fail('executed', 'simulator did not report a tick count '
                                  '(program did not run to completion)')
    v.ticks = int(mt.group(1))
    mn = _NONNULL_RE.search(out)
    mz = _NULL_RE.search(out)
    v.non_null = int(mn.group(1)) if mn else None
    v.null = int(mz.group(1)) if mz else None
    v.checks['executed'] = True

    # ── check 5: the comparison ACTUALLY HAPPENED, n_results times ────────────
    # This is the check that makes a missing reference fail instead of pass:
    # no reference => no PostCondition lines => zero comparisons => FAIL.
    v.postconditions = len(_POST_RE.findall(out)) + len(_POSTERR_RE.findall(out))
    if v.postconditions != n_results:
        return v.fail('compared',
                      f'{v.postconditions} PostCondition comparisons performed, '
                      f'{n_results} declared')
    v.checks['compared'] = True

    # ── check 6: no comparison FAILED (exit status is not evidence: mcode_run
    #             exits 0 on a failed PostCondition -- confirmed 2026-07-31) ───
    v.errors = [ln.strip() for ln in _POSTERR_RE.findall(out)]
    if v.errors:
        return v.fail('clean', f'{len(v.errors)} PostCondition mismatch(es): '
                               f'{v.errors[0][:120]}')
    v.checks['clean'] = True
    return v.passed()


def is_vectorized(source):
    """Whether the vectorizer commits a kernel for this source (reported next to
    each result so a green run is not mistaken for a vector run)."""
    try:
        import copy
        import pycparser
        from compiler import _FAKE_TYPEDEFS
        from ir import Temp
        from ir_gen import IRGenerator
        from dot_vectorizer import vectorize_all_module
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + source)
        Temp.reset()
        g = IRGenerator(global_base=0x400)
        g.visit(ast)
        _out, stats, _r = vectorize_all_module(copy.deepcopy(list(g.instructions)))
        return bool(stats.vectorized)
    except Exception:
        return None
