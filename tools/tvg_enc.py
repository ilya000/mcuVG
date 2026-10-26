#!/usr/bin/env python3
"""mcuVG - a TinyVG v1 encoder from svgfront shapes, written to MEASURE TinyVG against mcuVG on the same
pictures (README, 'Size').  Faithful to the spec's layout (16-bit range, the
scale svg2tvgt picks, RGBA8888 table, fill path / draw line path / outline fill path, arcs native with TinyVG's sweep);
what TinyVG cannot say is approximated the way a real converter would have to: 2-stop gradients (first and last stop),
strokes with round caps and joins, even-odd fill.

    python3 tools/tvg_enc.py IN.svg OUT.tvg
"""
import math
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import svgfront  # noqa: E402


def varuint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Enc:
    def __init__(self, doc):
        self.doc = doc
        w, h = doc.vb[2], doc.vb[3]
        m = max(w, h)
        self.scale = 0
        while self.scale < 15 and m * (1 << (self.scale + 2)) < 32768:   # svg2tvgt: max << (scale + 1) < 32768
            self.scale += 1
        self.colors = []

    def unit(self, v):
        r = int(round(v * (1 << self.scale)))
        return struct.pack("<h", max(-32768, min(32767, r)))

    def point(self, x, y):
        return self.unit(x) + self.unit(y)

    def color(self, c):
        if c not in self.colors:
            self.colors.append(c)
        return varuint(self.colors.index(c))

    def style(self, p):
        """-> (kind, payload)"""
        if p[0] == "flat":
            return 0, self.color(p[1])
        stops = p[3]
        c0, c1 = self.color(stops[0][1]), self.color(stops[-1][1])
        if p[0] == "linear":
            return 1, self.point(*p[1]) + self.point(*p[2]) + c0 + c1
        (cx, cy), r = p[1], p[2]
        return 2, self.point(cx, cy) + self.point(cx + r, cy) + c0 + c1

    def path(self, segs):
        """-> (segment count, bytes)"""
        subs = []
        cur = None
        for s in segs:
            if s[0] == "M":
                cur = [s]
                subs.append(cur)
            elif cur is None:
                continue
            else:
                cur.append(s)
        subs = [s for s in subs if len(s) > 1]
        counts, body = b"", b""
        for sub in subs:
            ins = b""
            n = 0
            x, y = sub[0][1], sub[0][2]
            sx, sy = x, y
            for s in sub[1:]:
                op = s[0]
                if op == "L":
                    if self.unit(s[2]) == self.unit(y):
                        ins += bytes([1]) + self.unit(s[1])
                    elif self.unit(s[1]) == self.unit(x):
                        ins += bytes([2]) + self.unit(s[2])
                    else:
                        ins += bytes([0]) + self.point(s[1], s[2])
                    x, y = s[1], s[2]
                elif op == "C":
                    ins += bytes([3]) + self.point(s[1], s[2]) + self.point(s[3], s[4]) + self.point(s[5], s[6])
                    x, y = s[5], s[6]
                elif op == "Q":
                    ins += bytes([7]) + self.point(s[1], s[2]) + self.point(s[3], s[4])
                    x, y = s[3], s[4]
                elif op == "A":
                    rx, ry, rot, large, sweep, ex, ey = s[1:]
                    flags = bytes([(1 if large else 0) | (0 if sweep else 2)])   # TinyVG's sweep is SVG's inverted
                    if abs(rx - ry) < 1e-6 * max(rx, ry, 1):
                        ins += bytes([4]) + flags + self.unit(rx) + self.point(ex, ey)
                    else:
                        ins += bytes([5]) + flags + self.unit(rx) + self.unit(ry) + self.unit(rot) + self.point(ex, ey)
                    x, y = ex, ey
                elif op == "Z":
                    ins += bytes([6])
                    x, y = sx, sy
                else:
                    continue
                n += 1
            counts += varuint(max(n, 1) - 1)
            body += self.point(sub[0][1], sub[0][2]) + ins
        return len(subs), counts + body

    def encode(self):
        cmds = b""
        for s in self.doc.shapes:
            nseg, pdata = self.path(s["segs"])
            if not nseg:
                continue
            f, st = s["fill"], s["stroke"]
            if f is not None and st is not None and nseg <= 64:
                fk, fp = self.style(f)
                sk, sp = self.style(st)
                cmds += bytes([10 | fk << 6]) + bytes([(nseg - 1) | sk << 6]) + fp + sp + self.unit(s["width"]) + pdata
                continue
            if f is not None:
                fk, fp = self.style(f)
                cmds += bytes([3 | fk << 6]) + varuint(nseg - 1) + fp + pdata
            if st is not None:
                sk, sp = self.style(st)
                cmds += bytes([7 | sk << 6]) + varuint(nseg - 1) + sp + self.unit(s["width"]) + pdata
        w, h = self.doc.vb[2], self.doc.vb[3]
        head = bytes([0x72, 0x56, 1, self.scale | 0 << 4 | 0 << 6]) + struct.pack("<HH", int(math.ceil(w)), int(math.ceil(h)))
        table = varuint(len(self.colors)) + b"".join(bytes(c) for c in self.colors)
        return head + table + cmds + b"\x00"


def encode(doc):
    return Enc(doc).encode()


if __name__ == "__main__":
    d = svgfront.load(sys.argv[1])
    b = encode(d)
    open(sys.argv[2], "wb").write(b)
    print(len(b), "B")
