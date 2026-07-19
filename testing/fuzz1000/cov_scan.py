#!/usr/bin/env python3
"""
cov_scan.py -- instruction/sub-instruction/datatype coverage auditor for the
fuzz1000 campaign. Coverage is MEASURED on the emitted .mcode of programs that
PASSED simulation, then compared against the checklist of everything the
compiler can emit ("leave nothing untouched" is verified, not assumed).

  cov_scan.py prog.mcode [...]   -> print one coverage key per line (accumulate)
  cov_scan.py --report acc_file  -> compare accumulated keys vs the checklist
"""
import re
import sys

# ── everything the compiler can emit (mnemonic × sub-op × type tag) ───────────
ALU_INT_OPS   = ['+', '-', '*', '/', '&', '|', '^', '~&', '~|', '~^', '<<', '>>']
ALU_FLOAT_OPS = ['+', '-', '*', '/']
CONDS         = ['==', '!=', '<', '<=', '>', '>=']

CHECKLIST = set()
# ── integer ALU ($i64; / also $u64 — the only op whose signedness the
#    compiler distinguishes besides >>; '%' is SYNTHETIC (/ * -) so only its
#    divide appears; '<' and '<=' branches are canonicalized to '>'/'>=' by
#    operand swap in codegen (codegen.py:521), so those forms cannot exist)
for op in ALU_INT_OPS:
    CHECKLIST.add(f"alu {op} $i64")
CHECKLIST.add("alu / $u64")
CHECKLIST.add("alu >> $u64")           # unsigned = logical shift
for op in ALU_FLOAT_OPS:
    CHECKLIST.add(f"alu {op} $f32")
    CHECKLIST.add(f"alu {op} $f64")
# ── loads/stores: floats are loaded/stored as raw-width integers (f64 via
#    $i64, f32 via $i32 high-half) — ($f32)/($f64) memory tags are never
#    emitted; $u64 loads come from unsigned long long
for t in ['$i8', '$i16', '$i32', '$i64', '$u8', '$u16', '$u32', '$u64']:
    CHECKLIST.add(f"ld {t}")
for t in ['$i8', '$i16', '$i32', '$i64']:
    CHECKLIST.add(f"st {t}")
# ── branches: 4 emitted comparison forms + the two test-tag widths float
#    compares use ($i32 for f32 sign-test, $i64 for f64/int)
for c in ['==', '!=', '>', '>=']:
    CHECKLIST.add(f"br {c}")
CHECKLIST.add("brtag $i32")
CHECKLIST.add("brtag $i64")
# ── casts: the compiler uses $i32 as the generic int tag on the int side of
#    int<->float casts; int-width truncation/extension casts use $i64<-$iN
for pair in ['$f64<-$i32', '$f32<-$i32', '$i32<-$f64', '$i32<-$f32',
             '$f64<-$f32', '$f32<-$f64',
             '$i64<-$i8', '$i64<-$i16', '$i64<-$i32',
             '$i64<-$u8', '$i64<-$u16']:
    CHECKLIST.add(f"cast {pair}")
CHECKLIST.add("fsqrt $f32"); CHECKLIST.add("fsqrt $f64")
for c in ['>', '<', '==', '!=', '>=', '<=']:
    CHECKLIST.add(f"cmov {c}")
CHECKLIST.add("slice"); CHECKLIST.add("pack"); CHECKLIST.add("set")
# ── vector ops carry $v-prefixed tags
for op in ['+', '-', '*']:
    for t in ['$vi8', '$vu8', '$vi16', '$vu16', '$vi32', '$vu32']:
        CHECKLIST.add(f"v {op} {t}")
CHECKLIST.add("v rep")
for t in ['$vi8', '$vu8', '$vi16', '$vu16']:
    CHECKLIST.add(f"dot {t}")
CHECKLIST.add("dot acc $vi8"); CHECKLIST.add("dot acc $vu16")
for op in ['+', '$max']:
    for t in ['$vi8', '$vu8', '$vi16', '$vu16']:
        CHECKLIST.add(f"vreduce {op} {t}")
CHECKLIST.add("vreduce + $vi32"); CHECKLIST.add("vreduce + $vu32")
CHECKLIST.add("call label"); CHECKLIST.add("call reg")
CHECKLIST.add("return"); CHECKLIST.add("halt"); CHECKLIST.add("null")


def scan(text):
    keys = set()
    for line in text.split('\n'):
        t = line.strip()
        if not t or t.endswith(':') or t in ('||', ';') or t.startswith('//'):
            continue
        m = re.match(r'\$ld\s+\((\$\w+)\)', t)
        if m: keys.add(f"ld {m.group(1)}"); continue
        m = re.match(r'\$st\s+\((\$\w+)\)', t)
        if m: keys.add(f"st {m.group(1)}"); continue
        m = re.match(r'\?\s+\((\$\w+)\)\s+\$r\d+\s+(\S+)\s+\$goto', t)
        if m:
            keys.add(f"br {m.group(2)}"); keys.add(f"brtag {m.group(1)}")
            continue
        m = re.match(r'\$cast\s+\((\$\w+)\)\s+\$r\d+\s+\((\$\w+)\)', t)
        if m: keys.add(f"cast {m.group(1)}<-{m.group(2)}"); continue
        m = re.match(r'\$fsqrt\s+\$r\d+\s+\((\$\w+)\)', t)
        if m: keys.add(f"fsqrt {m.group(1)}"); continue
        m = re.match(r'\$cmov\s+\?\s+\(\$\w+\)\s+\$r\d+\s+(\S+)', t)
        if m: keys.add(f"cmov {m.group(1)}"); continue
        m = re.match(r'\$vreduce\s+(\S+)\s+\$r\d+\s+\((\$\w+)\)', t)
        if m: keys.add(f"vreduce {m.group(1)} {m.group(2)}"); continue
        m = re.match(r'\$dot\s+(\$accumulate\s+)?\$r\d+\s+\((\$\w+)\)', t)
        if m:
            keys.add(f"dot acc {m.group(2)}" if m.group(1) else f"dot {m.group(2)}")
            continue
        m = re.match(r'\$v\s+(\S+)\s+\$r\d+\s+\((\$\w+)\)', t)
        if m:
            keys.add(f"v {m.group(1)} {m.group(2)}")
            if '$replicate' in t: keys.add("v rep")
            continue
        if t.startswith('$slice'): keys.add("slice"); continue
        if t.startswith('$pack'):  keys.add("pack"); continue
        if t.startswith('$set'):   keys.add("set"); continue
        if t.startswith('$call'):
            keys.add("call reg" if re.match(r'\$call\s+\$r\d+', t) else "call label")
            continue
        if t.startswith('$return'): keys.add("return"); continue
        if t.startswith('$halt'):   keys.add("halt"); continue
        if t.startswith('$null'):   keys.add("null"); continue
        m = re.match(r'([+\-*/<>&|^~]{1,3})\s+\$r\d+\s+\((\$\w+)\)', t)
        if m: keys.add(f"alu {m.group(1)} {m.group(2)}"); continue
    return keys


def main():
    if sys.argv[1] == '--report':
        seen = set()
        with open(sys.argv[2]) as f:
            for line in f:
                if line.strip():
                    seen.add(line.strip())
        missing = sorted(CHECKLIST - seen)
        extra   = sorted(seen - CHECKLIST)
        print(f"coverage: {len(CHECKLIST) - len(missing)}/{len(CHECKLIST)} "
              f"checklist items touched by PASSING programs")
        if missing:
            print("MISSING:")
            for k in missing:
                print(f"  {k}")
        if extra:
            print("extra (emitted, not in checklist):")
            for k in extra:
                print(f"  {k}")
        sys.exit(1 if missing else 0)
    keys = set()
    for path in sys.argv[1:]:
        with open(path) as f:
            keys |= scan(f.read())
    for k in sorted(keys):
        print(k)


if __name__ == '__main__':
    main()
