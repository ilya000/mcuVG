#!/usr/bin/env python3
"""mcuVG - the C decoder (src/mcuvg.c) against the reference (mcuvg.py decode): every picture
encoded, then drawn twice by the same rasteriser - once through the C decoder, once through the Python one - and the
pixels must be the same.  Also: cut pictures (every length short of whole) and random bytes must not crash and must
report an error.

    python3 tools/mcuvg_check.py [--px 240] [--size 112] SVG_OR_DIR ...
"""
import argparse
import glob
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import svgfront  # noqa: E402
import vg_host  # noqa: E402
import mcuvg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--px", type=float, default=240)
    ap.add_argument("--size", type=int, default=112)
    ap.add_argument("--cuts", type=int, default=40, help="pictures whose every prefix is fed to the decoder")
    a = ap.parse_args()
    files = []
    for p in a.inputs:
        files += sorted(glob.glob(os.path.join(p, "*.svg"))) if os.path.isdir(p) else [p]
    lib = vg_host.Lib()
    same = diff = 0
    worst = []
    for f in files:
        doc = svgfront.load(f)
        if not doc.shapes:
            continue
        b = mcuvg.encode(doc, a.px)
        ref, w, h = lib.draw(mcuvg.decode(b), a.size)
        k = a.size / doc.vb[2]
        ours, rc = lib.draw_mcuvg(b, w, h, k)
        if rc != 0:
            print("C decoder refused", f, rc)
            diff += 1
            continue
        if ours == ref:
            same += 1
        else:
            diff += 1
            n = sum(1 for x, y in zip(ours, ref) if x != y)
            worst.append((n, f))
    print(f"{same + diff} pictures: C decoder == Python decoder on {same}, different on {diff}")
    for n, f in sorted(worst, reverse=True)[:8]:
        print(f"  {n} bytes differ: {f}")
    # robustness: every prefix of some pictures, and random bytes after a good header
    rnd = random.Random(7)
    errs = total = 0
    picks = rnd.sample(files, min(a.cuts, len(files)))
    for f in picks:
        b = mcuvg.encode(svgfront.load(f), a.px)
        for cut in range(len(b)):
            _, rc = lib.draw_mcuvg(b[:cut], 64, 64, 0.25)
            errs += rc != 0
            total += 1
        for _ in range(20):
            junk = bytearray(b)
            for _ in range(rnd.randint(1, 6)):
                junk[rnd.randrange(3, len(junk))] = rnd.randrange(256)
            lib.draw_mcuvg(bytes(junk), 64, 64, 0.25)
    print(f"cut pictures: {errs} of {total} prefixes refused (all must be); damaged pictures drawn without a crash")
    sys.exit(1 if diff or errs != total else 0)


if __name__ == "__main__":
    main()
