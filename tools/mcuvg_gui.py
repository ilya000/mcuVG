#!/usr/bin/env python3
"""mcuVG - the converter in a window (Windows, macOS, Linux; Python 3 with tkinter, nothing else needed).

    python3 tools/mcuvg_gui.py [FILE.svg ...]

Add SVG files, set the largest size they will be shown at and the quality, press Convert: each FILE.mcuvg is written
next to its SVG (or into the chosen folder). The list shows the sizes, what mcuVG could not express, the memory a device
needs at that size (when a C compiler is around to build the rasteriser), and the picture as a line of Lua with the
base64 text form, ready to copy.
"""
import base64
import os
import sys
import tkinter as tk
from tkinter import filedialog, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcuvg  # noqa: E402
import svgfront  # noqa: E402


class App:
    def __init__(self, root, files):
        self.root = root
        self.files = list(files)
        self.out_dir = None
        self.lib = None
        self.lib_note = ""
        try:
            import vg_host
            self.lib = vg_host.Lib()
        except Exception as e:                  # no compiler: convert anyway
            self.lib_note = f"(memory figures off: {e})"
        root.title("mcuVG converter")
        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Add SVG files...", command=self.add).pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear).pack(side="left", padx=4)
        ttk.Button(top, text="Output folder...", command=self.pick_out).pack(side="left", padx=4)
        self.out_label = ttk.Label(top, text="output: next to each SVG")
        self.out_label.pack(side="left", padx=8)
        opts = ttk.Frame(root, padding=(8, 0))
        opts.pack(fill="x")
        ttk.Label(opts, text="Shown at (px):").pack(side="left")
        self.px = tk.StringVar(value="240")
        ttk.Entry(opts, textvariable=self.px, width=6).pack(side="left", padx=4)
        ttk.Label(opts, text="Quality:").pack(side="left", padx=(12, 0))
        self.quality = tk.IntVar(value=100)
        ttk.Scale(opts, from_=10, to=100, orient="horizontal", length=200,
                  command=lambda v: self.quality.set(int(float(v)))).pack(side="left", padx=4)
        ttk.Label(opts, textvariable=self.quality, width=4).pack(side="left")
        ttk.Label(opts, text="%").pack(side="left")
        self.v1 = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Version 1 only (a device without clips, shadows, blur)",
                        variable=self.v1).pack(side="left", padx=(12, 0))
        ttk.Button(opts, text="Convert", command=self.convert).pack(side="right")
        self.text = tk.Text(root, width=110, height=28, wrap="none", font=("Courier", 11))
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        self.show()

    def add(self):
        got = filedialog.askopenfilenames(title="SVG files", filetypes=[("SVG", "*.svg"), ("All files", "*.*")])
        self.files += [f for f in got if f not in self.files]
        self.show()

    def clear(self):
        self.files = []
        self.show()

    def pick_out(self):
        d = filedialog.askdirectory(title="Where to write the .mcuvg files")
        if d:
            self.out_dir = d
            self.out_label.config(text=f"output: {d}")

    def show(self, lines=None):
        self.text.delete("1.0", "end")
        if lines is None:
            lines = [f"{len(self.files)} file(s):"] + ["  " + f for f in self.files]
            if self.lib_note:
                lines.append(self.lib_note)
        self.text.insert("end", "\n".join(lines) + "\n")

    def convert(self):
        try:
            px = max(8.0, float(self.px.get()))
        except ValueError:
            px = 240.0
        q = self.quality.get()
        lines = [f"shown at {px:g} px, quality {q} % (grid {mcuvg.quality_prec(q):.2f} px)", ""]
        total_svg = total_out = 0
        for f in self.files:
            try:
                doc = svgfront.load(f)
                b = mcuvg.encode(doc, px, quality=q, maxver=1 if self.v1.get() else mcuvg.VERSION)
            except Exception as e:
                lines.append(f"{os.path.basename(f)}: not converted: {e}")
                continue
            d = self.out_dir or os.path.dirname(os.path.abspath(f))
            out = os.path.join(d, os.path.splitext(os.path.basename(f))[0] + ".mcuvg")
            with open(out, "wb") as o:
                o.write(b)
            total_svg += os.path.getsize(f)
            total_out += len(b)
            line = f"{os.path.basename(f)}: {os.path.getsize(f)} B -> {len(b)} B, version {b[2]}"
            if self.lib is not None:
                size = int(round(px))
                k = size / max(doc.vb[2], doc.vb[3])
                w, h = max(1, int(round(doc.vb[2] * k))), max(1, int(round(doc.vb[3] * k)))
                _, rc = self.lib.draw_mcuvg(b, w, h, k)
                pts, edges = self.lib.usage()
                line += f", device memory at {size} px: {self.lib.block_size(w, pts, edges)} B"
                if b[2] >= 2:
                    line += f" + layers {self.lib.layers_used()} B"
            lines.append(line)
            for what, how in doc.converted:
                lines.append(f"    converted {what}: {how}")
            for what, why in doc.dropped:
                lines.append(f"    dropped {what}: {why}")
            lines.append(f'    mcuvg.draw("mcuvg:{base64.b64encode(b).decode()}")')
        if total_svg:
            lines += ["", f"all: {total_svg} B of SVG -> {total_out} B of mcuVG ({100 * total_out / total_svg:.0f} %)"]
        self.show(lines)


def main():
    root = tk.Tk()
    App(root, [f for f in sys.argv[1:] if f.lower().endswith(".svg")])
    root.mainloop()


if __name__ == "__main__":
    main()
