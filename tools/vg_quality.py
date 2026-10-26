#!/usr/bin/env python3
"""mcuVG - what the quality setting buys: for each quality, the mean picture size and how far the picture drawn by the
C rasteriser is from resvg's drawing of the original SVG.

    python3 tools/vg_quality.py [--px 240] [--draw 112] [--q 100,75,50,25] DIR ...
"""
import argparse
import glob
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcuvg  # noqa: E402
import svgfront  # noqa: E402
import vg_host  # noqa: E402

NODE = os.environ.get("NODE", "node")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--px", type=float, default=240)
    ap.add_argument("--draw", type=int, default=112)
    ap.add_argument("--q", default="100,75,50,25,10")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    files = []
    for d in a.dirs:
        files += sorted(glob.glob(os.path.join(d, "*.svg")))[: a.limit or None]
    tmp = tempfile.mkdtemp(prefix="vgq")
    args = []
    for i, f in enumerate(files):
        args += [f, os.path.join(tmp, f"{i}.png")]
    for k in range(0, len(args), 400):
        ch = args[k:k + 400]
        subprocess.run([NODE, os.path.join(HERE, "ref_render.mjs"), ch[0], ch[1], str(a.draw)] + ch[2:], check=True)
    refs, docs = [], []
    for i, f in enumerate(files):
        r = np.asarray(Image.open(os.path.join(tmp, f"{i}.png")).convert("RGBA")).astype(np.float32)
        refs.append(np.concatenate([r[..., :3] * r[..., 3:4] / 255.0, r[..., 3:4]], axis=-1))
        docs.append(svgfront.load(f))
    lib = vg_host.Lib()
    print(f"{len(files)} pictures, grid for {a.px:g} px, drawn at {a.draw} px; SVG mean "
          f"{np.mean([os.path.getsize(f) for f in files]):.0f} B")
    print(f"{'quality':>7s} {'grid px':>8s} {'bytes':>7s} {'vs 100':>7s} {'diff':>6s} {'bad %':>7s} {'worst bad %':>11s}")
    base = None
    for q in [float(x) for x in a.q.split(",")]:
        size, diffs, bads = 0, [], []
        for doc, ref in zip(docs, refs):
            b = mcuvg.encode(doc, a.px, quality=q)
            size += len(b)
            img, w, h = lib.draw(mcuvg.decode(b), a.draw)
            ours = np.frombuffer(img, np.uint8).reshape(h, w, 4).astype(np.float32)
            d = np.abs(ours - ref[:h, :w])
            diffs.append(float(d.mean()))
            bads.append(float((d.max(axis=-1) > 32).mean()))
        mean = size / len(docs)
        base = base or mean
        print(f"{q:7.0f} {mcuvg.quality_prec(q):8.2f} {mean:7.0f} {100 * mean / base:6.0f}% {np.mean(diffs):6.3f} "
              f"{100 * np.mean(bads):7.3f} {100 * max(bads):11.3f}")


if __name__ == "__main__":
    main()
