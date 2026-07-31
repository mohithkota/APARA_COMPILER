"""
suite.py -- the R6.2A vector regression suite: one program per (packed marker,
kernel family), each with a real golden reference.

--------------------------------------------------------------------------------
RULES EVERY PROGRAM HERE OBEYS
--------------------------------------------------------------------------------
* it declares a global `results[]`, which is the convention `try_golden_verify`
  keys on to produce an independent gcc reference;
* it INITIALISES every array it reads, in a loop, with deterministic values.
  This is not cosmetic: the same source is compiled twice, once by gcc and once
  by this compiler, and an uninitialised local holds different garbage in each.
  Comparing two runs over undefined data is another way to get a meaningless
  green result;
* the values stay well inside the element type's range so that neither build
  relies on overflow behaviour (signed overflow is undefined in C, and the
  point of the reference is to be beyond argument);
* it declares how many results it writes, so the harness can require exactly
  that many comparisons to actually take place.

The markers themselves are the reason this suite exists. `vi8_t`/`vu16_t`/... are
compiler-only layout markers; before R6.2A only `vu8_t` was declared in
`golden_stubs.h`, so every other marker silently lost its reference.
"""

# Every packed marker the compiler defines (compiler.py _FAKE_TYPEDEFS).
# NOTE: there is no `vi64_t`. A 64-bit element gives a one-lane "vector", so the
# compiler deliberately defines no such marker (vector_capability_db.ELEMENT_TYPES
# stops at 32 bits); declaring one in golden_stubs.h alone would let a test
# compile natively and then fail to parse on the APARA side.
MARKERS = ['vi8_t', 'vu8_t', 'vi16_t', 'vu16_t', 'vi32_t', 'vu32_t']

# Element counts kept small enough that the whole suite simulates quickly, and
# a multiple of 8 so the 8-lane vi8 kernels have no remainder to peel.
N = 64


def _init(T, name, n, expr):
    return f"for(i=0;i<{n};i++) {name}[i] = ({T})({expr});"


def _prog(body, n_results):
    return (f"long long results[{n_results}];\n"
            f"int main(void) {{\n{body}\n  return 0;\n}}\n")


def elementwise(T):
    return _prog(f"""  {T} a[{N}],b[{N}],c[{N}]; int i;
  {_init(T, 'a', N, 'i & 7')}
  {_init(T, 'b', N, '(i & 3) + 1')}
  for(i=0;i<{N};i++) c[i] = a[i] + b[i];
  results[0]=c[0]; results[1]=c[9]; results[2]=c[33]; results[3]=c[{N - 1}];""", 4)


def axpy(T):
    return _prog(f"""  {T} X[{N}],Y[{N}]; int i; int a = 3;
  {_init(T, 'X', N, 'i & 7')}
  {_init(T, 'Y', N, 'i & 3')}
  for(i=0;i<{N};i++) Y[i] += a * X[i];
  results[0]=Y[0]; results[1]=Y[9]; results[2]=Y[33]; results[3]=Y[{N - 1}];""", 4)


def dot(T):
    return _prog(f"""  {T} a[{N}],b[{N}]; int i; long long s = 0;
  {_init(T, 'a', N, 'i & 7')}
  {_init(T, 'b', N, '(i & 3) + 1')}
  for(i=0;i<{N};i++) s += a[i] * b[i];
  results[0]=s; results[1]=a[0]; results[2]=b[{N - 1}];""", 3)


def reduction(T):
    return _prog(f"""  {T} a[{N}]; int i; long long s = 0;
  {_init(T, 'a', N, 'i & 7')}
  for(i=0;i<{N};i++) s += a[i];
  results[0]=s; results[1]=a[{N - 1}];""", 2)


def conv3(T):
    return _prog(f"""  {T} in[{N + 8}],out[{N + 8}]; int i;
  {_init(T, 'in', N + 8, 'i & 7')}
  for(i=0;i<{N - 3};i++) out[i] = in[i] + in[i+1] + in[i+2];
  results[0]=out[0]; results[1]=out[11]; results[2]=out[{N - 4}];""", 3)


def gemm(T):
    M = 16
    return _prog(f"""  {T} A[{M * M}],B[{M * M}],C[{M * M}]; int i,j,k,s;
  for(i=0;i<{M * M};i++) {{ A[i] = ({T})(i & 3); B[i] = ({T})((i & 7) + 1); C[i] = 0; }}
  for(i=0;i<{M};i++) for(k=0;k<{M};k++) {{ s = A[i*{M}+k];
    for(j=0;j<{M};j++) C[i*{M}+j] += s * B[k*{M}+j]; }}
  results[0]=C[0]; results[1]=C[17]; results[2]=C[{M * M - 1}];""", 3)


FAMILIES = [
    ('elementwise', elementwise, 4),
    ('axpy',        axpy,        4),
    ('dot',         dot,         3),
    ('reduction',   reduction,   2),
    ('conv3',       conv3,       3),
    ('gemm',        gemm,        3),
]

# Deliberately scalar controls: they exercise the harness on code the vectorizer
# never touches, so a change in the vector path can be told apart from a change
# in the compiler as a whole.
SCALAR = [
    ('scalar bubblesort', 3,
     _prog("""  int a[32]; int i,j,t;
  for(i=0;i<32;i++) a[i] = 32 - i;
  for(i=0;i<31;i++) for(j=0;j<31-i;j++)
    if(a[j] > a[j+1]) { t=a[j]; a[j]=a[j+1]; a[j+1]=t; }
  results[0]=a[0]; results[1]=a[15]; results[2]=a[31];""", 3)),
    ('scalar divmod', 2,
     _prog("""  int i; long long s = 0, p = 0;
  for(i=1;i<64;i++) { s += 1000 / i; p += 1000 % i; }
  results[0]=s; results[1]=p;""", 2)),
]


def build_suite(markers=None, families=None):
    """[(name, marker, family, n_results, source)] for the whole suite."""
    markers = markers or MARKERS
    fams = families or [f[0] for f in FAMILIES]
    out = []
    for T in markers:
        for (fam, fn, n) in FAMILIES:
            if fam not in fams:
                continue
            out.append((f"{fam} {T[:-2]}", T, fam, n, fn(T)))
    for (name, n, src) in SCALAR:
        out.append((name, 'int', 'scalar', n, src))
    return out
