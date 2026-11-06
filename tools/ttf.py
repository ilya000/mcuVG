#!/usr/bin/env python3
"""mcuVG - TrueType outlines for the converter's text: an SVG <text> becomes ordinary paths before encoding, so the format
needs no text and the device no font.

    python3 tools/ttf.py FONT.ttf|FONT.ttc "Text" [--face N]     what a string turns into (glyphs, advance, segments)
    python3 tools/ttf.py --find "Family" [--bold] [--italic]      which font file a family resolves to

Pure Python, no dependency: the font's directory, `head`, `maxp`, `hhea`, `hmtx`, `cmap` (formats 4 and 12), `loca`,
`glyf` (simple and composite glyphs) and `name`.  TrueType outlines only: a font with CFF outlines (most .otf) is
reported, not drawn.  No kerning, ligatures or shaping: one glyph a character, advanced by its width - enough for
labels on icons, not for running text in scripts that need shaping.

Fonts are looked up by family in MCUVG_FONTS (a path list) and then the system's font folders (macOS, Windows, Linux).
The generic families map to common fonts: sans-serif -> Helvetica / Arial / DejaVu Sans / Liberation Sans, serif ->
Times / Times New Roman / DejaVu Serif, monospace -> Courier / Courier New / DejaVu Sans Mono.
"""
import os
import struct
import sys

GENERIC = {
    "sans-serif": ["Helvetica", "Arial", "DejaVu Sans", "Liberation Sans", "Noto Sans"],
    "serif": ["Times", "Times New Roman", "DejaVu Serif", "Liberation Serif", "Noto Serif"],
    "monospace": ["Courier", "Courier New", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono"],
}
DIRS = ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"), "C:\\Windows\\Fonts", "/usr/share/fonts", "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts")]


class FontError(Exception):
    pass


class Font:
    """one face of a .ttf, or face `index` of a .ttc"""

    def __init__(self, path, index=0):
        self.path, self.index = path, index
        b = self.b = open(path, "rb").read()
        off = 0
        if b[:4] == b"ttcf":
            n = struct.unpack_from(">I", b, 8)[0]
            if index >= n:
                raise FontError(f"{path}: face {index} of {n}")
            off = struct.unpack_from(">I", b, 12 + 4 * index)[0]
        tag = b[off:off + 4]
        if tag == b"OTTO":
            raise FontError(f"{path}: CFF outlines (an OpenType .otf) - choose a TrueType font")
        if tag not in (b"\x00\x01\x00\x00", b"true"):
            raise FontError(f"{path}: not a TrueType font")
        nt = struct.unpack_from(">H", b, off + 4)[0]
        self.t = {}
        for i in range(nt):
            tg, _sum, o, ln = struct.unpack_from(">4sIII", b, off + 12 + 16 * i)
            self.t[tg.decode("latin-1")] = (o, ln)
        for need in ("head", "maxp", "hhea", "hmtx", "cmap", "loca", "glyf"):
            if need not in self.t:
                raise FontError(f"{path}: no '{need}' table" + (" (CFF outlines?)" if need in ("loca", "glyf") else ""))
        h = self.t["head"][0]
        self.upem = struct.unpack_from(">H", b, h + 18)[0]
        self.loca_long = struct.unpack_from(">h", b, h + 50)[0] == 1
        self.nglyphs = struct.unpack_from(">H", b, self.t["maxp"][0] + 4)[0]
        hh = self.t["hhea"][0]
        self.ascent, self.descent = struct.unpack_from(">hh", b, hh + 4)
        self.nhm = struct.unpack_from(">H", b, hh + 34)[0]
        self.cmap = self._cmap()
        self.family, self.style = self._names()

    # ---- tables ------------------------------------------------------------------------------------------------------
    def _names(self):
        if "name" not in self.t:
            return "", ""
        b, o = self.b, self.t["name"][0]
        _fmt, count, so = struct.unpack_from(">HHH", b, o)
        got = {}
        for i in range(count):
            pid, eid, lang, nid, ln, off = struct.unpack_from(">HHHHHH", b, o + 6 + 12 * i)
            if nid not in (1, 2, 16, 17):
                continue
            raw = b[o + so + off:o + so + off + ln]
            if pid == 3 or pid == 0:
                s = raw.decode("utf-16-be", "replace")
            elif pid == 1 and eid == 0:
                s = raw.decode("mac_roman", "replace")
            else:
                continue
            pref = (pid == 3 and lang == 0x409) or (pid == 1 and lang == 0)
            if nid not in got or pref:
                got[nid] = s
        return got.get(16, got.get(1, "")), got.get(17, got.get(2, ""))

    def _cmap(self):
        b, o = self.b, self.t["cmap"][0]
        n = struct.unpack_from(">H", b, o + 2)[0]
        best = None
        for i in range(n):
            pid, eid, so = struct.unpack_from(">HHI", b, o + 4 + 8 * i)
            fmt = struct.unpack_from(">H", b, o + so)[0]
            rank = {(3, 10, 12): 0, (0, 4, 12): 0, (0, 6, 12): 0, (3, 1, 4): 1, (0, 3, 4): 1, (0, 1, 4): 2,
                    (0, 0, 4): 2}.get((pid, eid, fmt))
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, o + so, fmt)
        if best is None:
            raise FontError(f"{self.path}: no Unicode character map")
        m = {}
        _, s, fmt = best
        if fmt == 4:
            segx2 = struct.unpack_from(">H", b, s + 6)[0]
            seg = segx2 // 2
            ends = struct.unpack_from(">%dH" % seg, b, s + 14)
            starts = struct.unpack_from(">%dH" % seg, b, s + 16 + segx2)
            deltas = struct.unpack_from(">%dh" % seg, b, s + 16 + 2 * segx2)
            ro = s + 16 + 3 * segx2
            ranges = struct.unpack_from(">%dH" % seg, b, ro)
            for k in range(seg):
                for c in range(starts[k], ends[k] + 1):
                    if c == 0xFFFF:
                        continue
                    if ranges[k] == 0:
                        g = (c + deltas[k]) & 0xFFFF
                    else:
                        a = ro + 2 * k + ranges[k] + 2 * (c - starts[k])
                        g = struct.unpack_from(">H", b, a)[0]
                        if g:
                            g = (g + deltas[k]) & 0xFFFF
                    if g:
                        m[c] = g
        else:
            ng = struct.unpack_from(">I", b, s + 12)[0]
            for k in range(ng):
                c0, c1, g0 = struct.unpack_from(">III", b, s + 16 + 12 * k)
                for c in range(c0, c1 + 1):
                    m[c] = g0 + c - c0
        return m

    def advance(self, g):
        o = self.t["hmtx"][0]
        i = min(g, self.nhm - 1)
        return struct.unpack_from(">H", self.b, o + 4 * i)[0]

    def _glyph_range(self, g):
        lo = self.t["loca"][0]
        if self.loca_long:
            a, z = struct.unpack_from(">II", self.b, lo + 4 * g)
        else:
            a, z = (2 * v for v in struct.unpack_from(">HH", self.b, lo + 2 * g))
        return self.t["glyf"][0] + a, z - a

    # ---- outlines ----------------------------------------------------------------------------------------------------
    def contours(self, g, depth=0):
        """glyph g -> contours, each a list of (x, y, on_curve) in font units"""
        if g >= self.nglyphs or depth > 8:
            return []
        o, ln = self._glyph_range(g)
        if ln == 0:
            return []
        b = self.b
        nc = struct.unpack_from(">h", b, o)[0]
        if nc >= 0:
            ends = struct.unpack_from(">%dH" % nc, b, o + 10)
            npts = ends[-1] + 1 if nc else 0
            p = o + 10 + 2 * nc
            il = struct.unpack_from(">H", b, p)[0]
            p += 2 + il
            flags = []
            while len(flags) < npts:
                f = b[p]
                p += 1
                flags.append(f)
                if f & 8:
                    r = b[p]
                    p += 1
                    flags += [f] * r
            xs, x = [], 0
            for f in flags:
                if f & 2:
                    d = b[p]
                    p += 1
                    x += d if f & 16 else -d
                elif not f & 16:
                    x += struct.unpack_from(">h", b, p)[0]
                    p += 2
                xs.append(x)
            ys, y = [], 0
            for f in flags:
                if f & 4:
                    d = b[p]
                    p += 1
                    y += d if f & 32 else -d
                elif not f & 32:
                    y += struct.unpack_from(">h", b, p)[0]
                    p += 2
                ys.append(y)
            out, s = [], 0
            for e in ends:
                out.append([(xs[i], ys[i], bool(flags[i] & 1)) for i in range(s, e + 1)])
                s = e + 1
            return out
        # a composite glyph
        out, p = [], o + 10
        while True:
            fl, gi = struct.unpack_from(">HH", b, p)
            p += 4
            if fl & 1:
                a1, a2 = struct.unpack_from(">hh", b, p)
                p += 4
            else:
                a1, a2 = struct.unpack_from(">bb", b, p)
                p += 2
            m = (1.0, 0.0, 0.0, 1.0)
            if fl & 8:
                s = struct.unpack_from(">h", b, p)[0] / 16384.0
                p += 2
                m = (s, 0.0, 0.0, s)
            elif fl & 0x40:
                sx, sy = (v / 16384.0 for v in struct.unpack_from(">hh", b, p))
                p += 4
                m = (sx, 0.0, 0.0, sy)
            elif fl & 0x80:
                m = tuple(v / 16384.0 for v in struct.unpack_from(">hhhh", b, p))
                p += 8
            dx, dy = (a1, a2) if fl & 2 else (0, 0)       # point matching (not xy offsets) is rare: placed at 0
            for c in self.contours(gi, depth + 1):
                out.append([(m[0] * x + m[2] * y + dx, m[1] * x + m[3] * y + dy, on) for x, y, on in c])
            if not fl & 0x20:
                break
        return out


def contour_segs(c, k, x0, y0):
    """one TrueType contour -> path segments ('M', 'L', 'Q', 'Z'), scaled by k, y flipped, at (x0, y0)"""
    pts = [(x0 + x * k, y0 - y * k, on) for x, y, on in c]
    n = len(pts)
    if n == 0:
        return []
    start = next((i for i in range(n) if pts[i][2]), None)
    if start is None:                         # all off-curve: start at the midpoint of the first two
        a, b2 = pts[0], pts[1 % n]
        pts.insert(0, ((a[0] + b2[0]) / 2, (a[1] + b2[1]) / 2, True))
        start, n = 0, n + 1
    pts = pts[start:] + pts[:start]
    segs = [("M", pts[0][0], pts[0][1])]
    ctrl = None
    for i in range(1, n + 1):
        x, y, on = pts[i % n]
        if on:
            segs.append(("Q", ctrl[0], ctrl[1], x, y) if ctrl else ("L", x, y))
            ctrl = None
        else:
            if ctrl:
                mx, my = (ctrl[0] + x) / 2, (ctrl[1] + y) / 2
                segs.append(("Q", ctrl[0], ctrl[1], mx, my))
            ctrl = (x, y)
    segs.append(("Z",))
    return segs


def text_segs(font, s, x, y, size, anchor="start", spacing=0.0):
    """the string's outlines at (x, y) baseline, size px per em; anchor start / middle / end -> (segs, advance, missing)"""
    k = size / font.upem
    total = 0.0
    glyphs, missing = [], []
    for ch in s:
        g = font.cmap.get(ord(ch), 0)
        if g == 0 and not ch.isspace():
            missing.append(ch)
        glyphs.append(g)
        total += font.advance(g) * k + spacing
    if glyphs:
        total -= spacing
    if anchor == "middle":
        x -= total / 2
    elif anchor == "end":
        x -= total
    segs, pen = [], x
    for g in glyphs:
        for c in font.contours(g):
            segs += contour_segs(c, k, pen, y)
        pen += font.advance(g) * k + spacing
    return segs, total, missing


_CACHE = {}


def _faces(path):
    if path in _CACHE:
        return _CACHE[path]
    out = []
    try:
        b = open(path, "rb").read(16)
        n = struct.unpack_from(">I", b, 8)[0] if b[:4] == b"ttcf" else 1
        for i in range(n):
            try:
                f = Font(path, i)
                out.append((f.family.lower(), f.style.lower(), i))
            except (FontError, struct.error, IndexError):
                pass
    except (OSError, struct.error):
        pass
    _CACHE[path] = out
    return out


def find(families, bold=False, italic=False):
    """the first family of the list found as a TrueType font -> Font, or None"""
    names = []
    for fam in families:
        fam = fam.strip().strip("'\"")
        names += GENERIC.get(fam.lower(), [fam])
    dirs = [d for d in os.environ.get("MCUVG_FONTS", "").split(os.pathsep) if d] + DIRS
    files = []
    for d in dirs:
        if os.path.isdir(d):
            for root, _ds, fs in os.walk(d):
                files += [os.path.join(root, f) for f in fs if f.lower().endswith((".ttf", ".ttc"))]
    want = ("bold" if bold else "") + (" italic" if italic else "")
    for name in names:
        hits = [(p, st, i) for p in files for fam, st, i in _faces(p) if fam == name.lower()]
        if not hits:
            continue

        def score(h):
            st = h[1]
            return (("bold" in st) != bold) * 2 + (("italic" in st or "oblique" in st) != italic) * 2 + \
                (st not in ("regular", "roman", "book", want.strip()))
        p, _st, i = min(hits, key=score)
        return Font(p, i)
    return None


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if a[0] == "--find":
        f = find([a[1]], "--bold" in a, "--italic" in a)
        print(f"{a[1]}: " + (f"{f.path} face {f.index} ({f.family} {f.style})" if f else "not found"))
        return 0 if f else 1
    f = Font(a[0], int(a[a.index("--face") + 1]) if "--face" in a else 0)
    segs, adv, miss = text_segs(f, a[1], 0, 0, 100)
    print(f"{f.family} {f.style}: {f.nglyphs} glyphs, {f.upem} units an em; '{a[1]}' at 100 px: advance {adv:.1f}, "
          f"{len(segs)} segments" + (f", missing {''.join(miss)!r}" if miss else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
