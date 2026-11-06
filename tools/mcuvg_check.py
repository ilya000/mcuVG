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
    # the version byte (SPEC.md 2.1): this decoder reads 1 .. its own (2); a newer one, 0, or a reserved flag bit is
    # refused - and a build without version 2 (MCUVG_V2 0, a version 1 device) refuses version 2 cleanly
    b = mcuvg.encode(svgfront.load(files[0]), a.px)
    vbad = []
    for what, i, v in (("version 0", 2, 0), ("version 3", 2, 3), ("version 255", 2, 255),
                       ("flag bit 4", 3, b[3] | 0x10), ("flag bit 5", 3, b[3] | 0x20), ("flag bit 7", 3, b[3] | 0x80)):
        x = bytearray(b)
        x[i] = v
        if lib.draw_mcuvg(bytes(x), 64, 64, 0.25)[1] == 0:
            vbad.append(what)
    ok = lib.draw_mcuvg(b, 64, 64, 0.25)[1] == 0
    x = bytearray(b)
    x[2] = 2
    ok = ok and lib.draw_mcuvg(bytes(x), 64, 64, 0.25)[1] == 0      # a version 1 body under a version 2 header
    v1 = vg_host.Lib(extra=("-DMCUVG_V2=0",))
    if v1.draw_mcuvg(bytes(x), 64, 64, 0.25)[1] == 0:
        vbad.append("version 2 by a version 1 build")
    if v1.draw_mcuvg(b, 64, 64, 0.25)[1] != 0:
        vbad.append("version 1 REFUSED by a version 1 build")
    # too little layer memory (a small device): a version 2 picture draws or says MCUVG_E_FULL (-1), never crashes
    small = []
    for f in files:
        b2 = mcuvg.encode(svgfront.load(f), a.px)
        if b2[2] < 2:
            continue
        for lim in (0, 1024, 16384):
            lib.layers_limit = lim
            rc = lib.draw_mcuvg(b2, 112, 112, 112 / max(1, svgfront.load(f).vb[2]))[1]
            if rc not in (0, -1):
                small.append(f"{os.path.basename(f)} at {lim} B: rc {rc}")
        lib.layers_limit = None
    print(f"little layer memory: {'every version 2 picture drawn or refused with E_FULL' if not small else 'WRONG: ' + ', '.join(small)}")
    vbad += small
    print(f"version byte: {'versions 1 and 2 drawn' if ok else 'version 1 or 2 REFUSED'}, "
          f"{'every newer version and reserved bit refused, a version 1 build refuses version 2' if not vbad else 'WRONG: ' + ', '.join(vbad)}")
    sys.exit(1 if diff or errs != total or vbad or not ok else 0)


if __name__ == "__main__":
    main()
