#!/usr/bin/env python3
"""mcuVG bench - the memcost plugin's per-instruction accesses folded into functions: which function makes the
loads and stores of each region (the stack's are spills and the saves of calls).   bypc.py <nm> <elf> <cost output>"""
import bisect
import re
import subprocess
import sys

nm, elf, out = sys.argv[1:4]
syms = []
for line in subprocess.run([nm, "-n", "-S", elf], capture_output=True, text=True, check=True).stdout.splitlines():
    p = line.split()
    if len(p) == 4 and p[2] in "tTwW":
        syms.append((int(p[0], 16), int(p[1], 16), p[3]))
syms.sort()
starts = [s[0] for s in syms]
regs = None
per = {}
for line in open(out):
    if line.startswith("seg ") and regs is None:
        t = line.split()
        regs = [t[i] for i in range(10, len(t), 3)]
    if not line.startswith("pc "):
        continue
    t = line.split()
    a = int(t[1], 16)
    v = list(map(int, t[2:]))
    i = bisect.bisect_right(starts, a) - 1
    name = syms[i][2] if i >= 0 and a < syms[i][0] + max(syms[i][1], 2) else "?"
    acc = per.setdefault(name, [0] * len(v))
    for k, x in enumerate(v):
        acc[k] += x
total = sum(sum(v) for v in per.values()) or 1
print("accesses by function (thousands): " + "  ".join(f"{r} ld/st" for r in regs))
for name, v in sorted(per.items(), key=lambda kv: -sum(kv[1]))[:14]:
    cols = "  ".join(f"{v[2 * k] / 1e3:7.0f}/{v[2 * k + 1] / 1e3:<7.0f}" for k in range(len(regs)))
    print(f"{100 * sum(v) / total:5.1f} %  {name:18s} {cols}")
