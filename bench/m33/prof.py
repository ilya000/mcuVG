#!/usr/bin/env python3
"""mcuVG bench - fold vg_bench's -DVG_PROF buckets (address, samples) into functions with the ELF's symbols
.   prof.py <nm> <elf> <prof output>"""
import bisect
import subprocess
import sys

nm, elf, out = sys.argv[1], sys.argv[2], sys.argv[3]
syms = []
for line in subprocess.run([nm, "-n", "-S", elf], capture_output=True, text=True, check=True).stdout.splitlines():
    p = line.split()
    if len(p) == 4 and p[2] in "tTwW":
        syms.append((int(p[0], 16), int(p[1], 16), p[3]))
syms.sort()
starts = [s[0] for s in syms]
per, total = {}, 0
for line in open(out):
    p = line.split()
    if len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p[0]):
        a, n = int(p[0], 16), int(p[1])
        total += n
        i = bisect.bisect_right(starts, a) - 1
        name = syms[i][2] if i >= 0 and a < syms[i][0] + max(syms[i][1], 2) else "?"
        per[name] = per.get(name, 0) + n
print(f"{total} samples")
for name, n in sorted(per.items(), key=lambda kv: -kv[1])[:16]:
    print(f"{100.0 * n / max(total, 1):6.1f} %  {n:7d}  {name}")
