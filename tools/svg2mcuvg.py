#!/usr/bin/env python3
"""mcuVG - the converter: SVG files -> .mcuvg pictures.

    python3 tools/svg2mcuvg.py [--px 240] [--quality 100] [--b64 | --lua] [--png] [-o OUT_DIR] FILE.svg ...

For every SVG: writes FILE.mcuvg next to it (or into OUT_DIR) and prints its size, what the SVG had that mcuVG cannot
say (masks, filters, text, images - reported, never drawn wrong), and the memory a device needs to draw it at --px:
the busiest path's points and edges, and the block size mcuvg_mem(width, points, edges) asks for.
  --px    the largest size the picture will be shown at, pixels (the coordinate grid is chosen from it)
  --quality  100 (default): a coordinate grid of 0.25 px at --px, invisible; lower = coarser grid and simpler outlines,
             smaller files (50: 0.5 px, about 8 % smaller on icons; 25: 1 px, about 16 % smaller, edges start to move)
  --b64   print the picture as a text string "mcuvg:<base64>" (SPEC.md section 5)
  --lua   print it as a Lua line: cube.vector("mcuvg:...")
  --png   also write FILE.png drawn at --px by the C rasteriser (the device's code), to look at
"""
import argparse
import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcuvg  # noqa: E402
import svgfront  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="SVG -> mcuVG")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--px", type=float, default=240)
    ap.add_argument("--quality", type=float, default=100)
    ap.add_argument("--b64", action="store_true")
    ap.add_argument("--lua", action="store_true")
    ap.add_argument("--png", action="store_true")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    lib = None
    try:
        import vg_host
        lib = vg_host.Lib()
    except Exception as e:                       # no C compiler: still convert, just skip the memory figure
        print(f"(memory figures off: {e})", file=sys.stderr)
    bad = 0
    for f in a.files:
        try:
            doc = svgfront.load(f)
        except Exception as e:
            print(f"{f}: not read: {e}")
            bad += 1
            continue
        b = mcuvg.encode(doc, a.px, quality=a.quality)
        stem = os.path.splitext(os.path.basename(f))[0]
        out_dir = a.out or os.path.dirname(os.path.abspath(f))
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, stem + ".mcuvg")
        with open(out, "wb") as o:
            o.write(b)
        line = f"{os.path.basename(f)}: {os.path.getsize(f)} B SVG -> {len(b)} B mcuVG ({out})"
        if lib is not None:
            size = int(round(a.px))
            k = size / max(doc.vb[2], doc.vb[3])
            w, h = max(1, int(round(doc.vb[2] * k))), max(1, int(round(doc.vb[3] * k)))
            raw, rc = lib.draw_mcuvg(b, w, h, k)
            pts, edges = lib.usage()
            line += f"; at {size} px: {pts} points, {edges} edges, block {lib.block_size(w, pts, edges)} B"
            if rc:
                line += f" [draw error {rc}]"
            if a.png:
                from PIL import Image
                im = Image.frombuffer("RGBA", (w, h), raw, "raw", "RGBa", 0, 1)
                im.save(os.path.join(out_dir, stem + ".png"))
        print(line)
        for what, why in doc.dropped:
            print(f"  dropped {what}: {why}")
        if a.b64 or a.lua:
            s = "mcuvg:" + base64.b64encode(b).decode()
            print(f'  cube.vector("{s}")' if a.lua else f"  {s}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
