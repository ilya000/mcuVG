#!/usr/bin/env python3
"""mcuVG - the C rasteriser against resvg, picture by picture.

    python3 tools/vg_check.py [--size 112] [--sheet OUT.png] [--worst 12] SVG_OR_DIR ...

Every SVG is drawn twice at the same size: by resvg (tools/ref_render.mjs, @resvg/resvg-js) and by
src/common/mcuvg/mcuvg_raster.c through vg_host.  The numbers are per channel of premultiplied RGBA, 0..255: the mean
absolute difference over the picture and the share of pixels off by more than 32 (a visible defect, not
anti-aliasing noise).  The sheet shows the worst pictures: reference, ours, and the difference x4.
Exit code 1 when a picture's mean is above --max-mean or its bad share above --max-bad.
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
import svgfront  # noqa: E402
import vg_host  # noqa: E402

NODE = os.environ.get("NODE", "node")


def premul(a):
    a = a.astype(np.float32)
    al = a[..., 3:4] / 255.0
    return np.concatenate([a[..., :3] * al, a[..., 3:4]], axis=-1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--size", type=int, default=112)
    ap.add_argument("--sheet")
    ap.add_argument("--worst", type=int, default=12)
    ap.add_argument("--max-mean", type=float, default=2.0)
    ap.add_argument("--max-bad", type=float, default=0.01)
    a = ap.parse_args()
    files = []
    for p in a.inputs:
        files += sorted(glob.glob(os.path.join(p, "*.svg"))) if os.path.isdir(p) else [p]
    lib = vg_host.Lib()
    tmp = tempfile.mkdtemp(prefix="vgcheck")
    args = []
    for i, f in enumerate(files):
        args += [f, os.path.join(tmp, f"{i}.png")]
    # resvg in batches (one node process renders many)
    for k in range(0, len(args), 400):
        chunk = args[k:k + 400]
        subprocess.run([NODE, os.path.join(HERE, "ref_render.mjs"), chunk[0], chunk[1], str(a.size)] + chunk[2:], check=True)
    rows, fails = [], 0
    for i, f in enumerate(files):
        ref = np.asarray(Image.open(os.path.join(tmp, f"{i}.png")).convert("RGBA"))
        doc = svgfront.load(f)
        raw, w, h = lib.draw(doc, a.size)
        ours = np.frombuffer(raw, np.uint8).reshape(h, w, 4).astype(np.float32)
        refp = premul(ref)[:h, :w]
        d = np.abs(ours - refp)
        mean = float(d.mean())
        bad = float((d.max(axis=-1) > 32).mean())
        rows.append((mean, bad, f, refp, ours, d, doc.dropped))
        if mean > a.max_mean or bad > a.max_bad:
            fails += 1
    rows.sort(key=lambda r: -(r[0] + 100 * r[1]))
    means = [r[0] for r in rows]
    bads = [r[1] for r in rows]
    print(f"{len(rows)} pictures at {a.size} px: mean |diff| {np.mean(means):.3f} (worst {max(means):.3f}), "
          f"pixels off > 32: {100 * np.mean(bads):.4f} % (worst {100 * max(bads):.3f} %), over the limits: {fails}")
    for mean, bad, f, *_rest, dropped in rows[:a.worst]:
        print(f"  {mean:6.3f} {100 * bad:7.3f} %  {os.path.relpath(f)}" + (f"  dropped: {len(dropped)}" if dropped else ""))
    if a.sheet:
        n = min(a.worst, len(rows))
        S = a.size
        sheet = Image.new("RGB", (3 * S + 20, n * (S + 4)), (40, 40, 48))
        for j, (mean, bad, f, refp, ours, d, _) in enumerate(rows[:n]):
            for k, im in enumerate((refp, ours, np.clip(d * 4, 0, 255))):
                rgb = im[..., :3] + (255 - im[..., 3:4]) * (40 / 255.0) if k < 2 else im[..., :3]
                sheet.paste(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB"), (k * (S + 10), j * (S + 4)))
        sheet.save(a.sheet)
        print("sheet:", a.sheet)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
