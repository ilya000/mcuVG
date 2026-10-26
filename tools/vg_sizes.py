#!/usr/bin/env python3
"""mcuVG - how many bytes a picture costs in each candidate format, over whole sets of SVGs.

    python3 tools/vg_sizes.py [--px 240] [--prec 0.25] [--draw 112] NAME=DIR ...

Per set: the mean size of the SVG as it is, gzipped, TinyVG (tvg_enc.py) and mcuVG (mcuvg.py) at the given grid; and the
quality of mcuVG after the round trip: decoded, drawn by the C rasteriser (vg_host) at --draw px and compared with resvg on
the original SVG (mean |diff| per channel 0..255; pixels off by more than 32).
"""
import argparse
import glob
import os
import subprocess
import sys
import tempfile
import zlib

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import svgfront  # noqa: E402
import tvg_enc  # noqa: E402
import vg_host  # noqa: E402
import mcuvg  # noqa: E402

NODE = os.environ.get("NODE", "node")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sets", nargs="+")
    ap.add_argument("--px", type=float, default=240)
    ap.add_argument("--prec", type=float, default=0.25)
    ap.add_argument("--draw", type=int, default=112)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    lib = vg_host.Lib()
    print(f"grid: a step <= {a.prec} px at {a.px:g} px; quality drawn at {a.draw} px")
    print(f"{'set':18s} {'n':>5s} {'SVG':>7s} {'gzip':>7s} {'TinyVG':>7s} {'mcuVG':>7s} {'mcuVG/TVG':>8s} {'diff':>6s} {'bad %':>7s}")
    for spec in a.sets:
        name, d = spec.split("=", 1)
        files = sorted(glob.glob(os.path.join(d, "*.svg")))
        if a.limit:
            files = files[:a.limit]
        tmp = tempfile.mkdtemp(prefix="vgsz")
        args = []
        for i, f in enumerate(files):
            args += [f, os.path.join(tmp, f"{i}.png")]
        for k in range(0, len(args), 400):
            ch = args[k:k + 400]
            subprocess.run([NODE, os.path.join(HERE, "ref_render.mjs"), ch[0], ch[1], str(a.draw)] + ch[2:], check=True)
        sv = gz = tv = wv = 0
        diffs, bads, n = [], [], 0
        for i, f in enumerate(files):
            raw = open(f, "rb").read()
            doc = svgfront.load(f)
            if not doc.shapes:
                continue
            n += 1
            sv += len(raw)
            gz += len(zlib.compress(raw, 9))
            tv += len(tvg_enc.encode(doc))
            b = mcuvg.encode(doc, a.px, a.prec)
            wv += len(b)
            back = mcuvg.decode(b)
            img, w, h = lib.draw(back, a.draw)
            ours = np.frombuffer(img, np.uint8).reshape(h, w, 4).astype(np.float32)
            ref = np.asarray(Image.open(os.path.join(tmp, f"{i}.png")).convert("RGBA")).astype(np.float32)
            ref = np.concatenate([ref[..., :3] * ref[..., 3:4] / 255.0, ref[..., 3:4]], axis=-1)[:h, :w]
            dd = np.abs(ours - ref)
            diffs.append(float(dd.mean()))
            bads.append(float((dd.max(axis=-1) > 32).mean()))
        print(f"{name:18s} {n:5d} {sv / n:7.0f} {gz / n:7.0f} {tv / n:7.0f} {wv / n:7.0f} {wv / tv:8.2f} "
              f"{np.mean(diffs):6.3f} {100 * np.mean(bads):7.3f}")


if __name__ == "__main__":
    main()
