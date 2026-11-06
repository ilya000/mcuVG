#!/usr/bin/env python3
"""mcuVG - MCU Vector Graphics (draft 1): encoder from svgfront shapes, and the reference decoder back to
shapes (for the checks: decode -> draw with the C rasteriser -> compare with resvg).  SPEC.md is the specification.

    python3 tools/mcuvg.py enc IN.svg OUT.mcuvg [--px 240] [--prec 0.25]
    python3 tools/mcuvg.py dump IN.mcuvg

Why its own.  The same picture has to serve a 112 px icon, a 240 px panel and a 480 px face, inside packages counted
in bytes.  Coordinates are DELTAS from the previous point as zig-zag varints (1 byte for a step under 64 units), in a
grid the encoder picks from the largest size the picture will be drawn at; commands are 3 bits each, packed; arcs are
native (icons are made of them); paints are a colour table plus up to 8-stop gradients; fills non-zero or even-odd;
strokes with caps, joins and a miter limit.  A shape can reuse the previous shape's outline (a shadow, a fill and an
outline of one path).

Layout (little-endian, varuint = 7 bits a byte, low first; zvar = zig-zag varuint):
    'm' 'V'   (0x6D 0x56)
    u8  version (1)
    u8  bits 0..3 F (the grid: 1 / 2^F of a design unit), 6 colours carry alpha; 4, 5, 7 reserved (0)
    varuint width, height                      design units
    varuint ncolors, then ncolors x RGB or RGBA
    shapes until a kind byte 0:
      u8 kind   bits 0..1 fill (0 none, 1 flat, 2 linear, 3 radial), 2..3 stroke (same), 4 even-odd, 5 the previous
                shape's path, 6..7 reserved (0)
      fill paint, stroke paint                 flat: varuint colour
                                               linear: zvar x0, y0 (absolute), zvar dx, dy (to the end), stops
                                               radial: zvar cx, cy (absolute), varuint r, stops
                                               stops: u8 n | 0x80 when the offsets are 0 and 1 and n == 2;
                                                      n x (u8 offset unless implied, varuint colour)
      stroke: varuint width (grid units), u8 cap (0..1) | join << 2 (2..3) | miter_x2 << 4 (miter limit = 1 + n / 2)
      path (unless bit 5): varuint ncmd, the commands' bits (low first) to a whole byte, then operands:
        0 L zvar dx dy   1 H zvar dx   2 V zvar dy   3 C zvar x3 (each point from the previous one)
        4 Q zvar x2      5 A: 3 more command bits (1 large, 2 sweep, 4 the last arc's radii); unless 4:
                         varuint r << 1 | ellipse [varuint ry, zvar rot deg]; then zvar dx dy
        6 M zvar dx dy (from the pen: the last point of anything before, the origin at first)   7 Z
      zvar: a signed number, one byte b < 240 for b - 120; 240..254 and one more byte for 120..2039 / -121..-2040;
            255 and a zig-zag varuint beyond
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import svgfront  # noqa: E402

VERSION = 2   # the newest format version written (only when a picture needs it: SPEC.md 2.1) and read
SPREAD = {"pad": 0, "reflect": 1, "repeat": 2}
SPREAD_N = {v: k for k, v in SPREAD.items()}
CAPS = {"butt": 0, "round": 1, "square": 2}
JOINS = {"miter": 0, "miter-clip": 0, "arcs": 0, "round": 1, "bevel": 2}
CAP_N = {v: k for k, v in CAPS.items() if k in ("butt", "round", "square")}
JOIN_N = {0: "miter", 1: "round", 2: "bevel"}


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


def zvar(v):
    """a signed number: one byte for -120..119 (b - 120), two for up to about +-2040 (b 240..254 and the next byte),
    and b 255 + a zig-zag varuint for anything larger"""
    if -120 <= v <= 119:
        return bytes([v + 120])
    if 120 <= v < 120 + 1920:
        i = v - 120
        return bytes([240 + (i >> 8), i & 255])
    if -121 - 1920 < v <= -121:
        i = 1920 + (-121 - v)
        return bytes([240 + (i >> 8), i & 255])
    return bytes([255]) + varuint((v << 1) if v >= 0 else ((-v << 1) - 1))


def arc_lambda(x0, y0, x1, y1, rx, ry, rot):
    """SVG's lambda: >= 1 means the radii do not reach, the centre is the chord's middle (a half ellipse at most)"""
    a = math.radians(rot)
    dx, dy = (x0 - x1) / 2, (y0 - y1) / 2
    xp, yp = math.cos(a) * dx + math.sin(a) * dy, -math.sin(a) * dx + math.cos(a) * dy
    return (xp / rx) ** 2 + (yp / ry) ** 2


def half_arc_fix(x0, y0, x1, y1, R, RY, rot, was_half):
    """an arc whose chord is (almost) its diameter: its centre moves with the square root of the chord's error, so a
    half ellipse whose ends were rounded a little closer swings by a pixel.  When the rounded chord falls short of the
    diameter, shrink the radii just enough that it does not: SVG then scales them back up to the chord, the centre at
    its middle - the half ellipse it was"""
    if R <= 0 or RY <= 0 or not was_half:
        return R, RY
    lam = arc_lambda(x0, y0, x1, y1, R, RY, rot)
    if lam < 1.0:
        k = math.sqrt(lam)
        R2, RY2 = int(R * k), int(RY * k)
        while R2 > 1 and RY2 > 1 and arc_lambda(x0, y0, x1, y1, R2, RY2, rot) < 1.0:
            R2, RY2 = R2 - 1, RY2 - 1
        if R2 > 0 and RY2 > 0:
            return R2, RY2
    return R, RY


def grid_bits(doc, px, prec):
    """F such that a grid step is at most prec pixels when the picture's longer side is drawn at px pixels"""
    m = max(doc.vb[2], doc.vb[3]) or 1
    unit_px = px / m
    f = 0
    while f < 15 and unit_px / (1 << f) > prec:
        f += 1
    return f


def quality_prec(quality):
    """the grid step at the display size for a quality 1..100: 100 -> 0.25 px (invisible), 50 -> 0.5, 25 -> 1 px"""
    q = max(1.0, min(100.0, float(quality)))
    return 0.25 * 100.0 / q


def _pt_line_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _rdp(pts, tol):
    """Ramer-Douglas-Peucker on a polyline: the points that matter at tolerance tol (first and last kept)"""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, k = -1.0, -1
        for m in range(i + 1, j):
            d = _pt_line_dist(pts[m], pts[i], pts[j])
            if d > best:
                best, k = d, m
        if best > tol:
            keep[k] = True
            stack += [(i, k), (k, j)]
    return [p for p, kp in zip(pts, keep) if kp]


def simplify(segs, tol):
    """drop what cannot be seen at tolerance tol (design units): curves whose control points lie within tol of their
    chord become lines, nearly flat arcs become lines, and runs of lines lose the points within tol of the line through
    their neighbours (RDP).  The ends of every curve and subpath stay exactly where they were"""
    if tol <= 0:
        return segs
    out, cur, start = [], None, None
    run = []                                      # points of the current run of lines, cur first

    def flush():
        nonlocal run
        if len(run) > 1:
            for p in _rdp(run, tol)[1:]:
                out.append(("L", p[0], p[1]))
        run = []

    for s in segs:
        op = s[0]
        if op == "M":
            flush()
            out.append(s)
            cur = start = (s[1], s[2])
            run = [cur]
            continue
        if op == "Z":
            flush()
            out.append(s)
            cur = start
            run = [cur]
            continue
        end = (s[-2], s[-1])
        straight = False
        if op == "L":
            straight = True
        elif op == "Q":
            straight = _pt_line_dist((s[1], s[2]), cur, end) <= tol
        elif op == "C":
            straight = (_pt_line_dist((s[1], s[2]), cur, end) <= tol and _pt_line_dist((s[3], s[4]), cur, end) <= tol)
        elif op == "A":
            rx, ry = s[1], s[2]
            c = math.hypot(end[0] - cur[0], end[1] - cur[1])
            r = min(rx, ry)
            if not s[4] and r > 0 and c > 0:                       # small arc: its sagitta decides
                h = c / 2.0
                sag = r - math.sqrt(max(0.0, r * r - h * h)) if h < r else r
                straight = sag <= tol
        if straight:
            if not run:
                run = [cur]
            run.append(end)
        else:
            flush()
            out.append(s)
            run = [end]
        cur = end
    flush()
    return out


class Enc:
    def __init__(self, doc, px=240, prec=0.25, quality=None, maxver=VERSION):
        self.doc = doc
        self.maxver = maxver
        self.v2 = False                                  # a version 2 record or paint written: the file says 2
        self.last = None
        if quality is not None:
            prec = quality_prec(quality)
        self.F = grid_bits(doc, px, prec)
        m = max(doc.vb[2], doc.vb[3]) or 1
        self.tol = 0.5 * prec * m / px                   # simplification tolerance, design units: half a grid step
        self.k = 1 << self.F
        self.colors = []
        self.pen = (0, 0)
        self.last_r = None                               # the last arc's radii, carried across shapes like the pen

    def q(self, v):
        return int(round(v * self.k))

    def color(self, c):
        if c not in self.colors:
            self.colors.append(c)
        return varuint(self.colors.index(c))

    def stops(self, st, ext=b""):
        st = st[:8]
        x = 0x40 if ext else 0
        if len(st) == 2 and st[0][0] == 0 and st[1][0] == 1:
            return bytes([2 | 0x80 | x]) + ext + self.color(st[0][1]) + self.color(st[1][1])
        out = bytes([len(st) | x]) + ext
        for o, c in st:
            out += bytes([max(0, min(255, int(round(o * 255))))]) + self.color(c)
        return out

    def ext(self, p):
        """a gradient's version 2 extension byte and fields (b"" when it is version 1's)"""
        spread, focal, fr, ry, rot = svgfront.paint_ext(p)
        if spread == "pad" and focal is None and ry is None:
            return b""
        if self.maxver < 2:
            self.doc.dropped.append(("paint", "version 1: spread, focal point and ellipse dropped (pad, centre, circle)"))
            return b""
        self.v2 = True
        x, out = SPREAD[spread], b""
        if focal is not None:
            x |= 4
            out += zvar(self.q(focal[0])) + zvar(self.q(focal[1])) + varuint(max(0, self.q(fr)))
        if ry is not None:
            x |= 8
            out += varuint(max(0, self.q(ry))) + zvar(int(round(rot)) % 360)
        return bytes([x]) + out

    def paint(self, p):
        if p is None:
            return 0, b""
        if p[0] == "flat":
            return 1, self.color(p[1])
        if p[0] == "linear":
            x0, y0 = self.q(p[1][0]), self.q(p[1][1])
            x1, y1 = self.q(p[2][0]), self.q(p[2][1])
            return 2, zvar(x0) + zvar(y0) + zvar(x1 - x0) + zvar(y1 - y0) + self.stops(p[3], self.ext(p))
        cx, cy = self.q(p[1][0]), self.q(p[1][1])
        r = p[2]
        e = self.ext(p)
        if not e and self.maxver < 2 and len(p) > 7 and p[7] is not None:
            r = (p[2] + p[7]) / 2                     # version 1: an ellipse drawn as a circle of the mean radius
        return 3, zvar(cx) + zvar(cy) + varuint(max(0, self.q(r))) + self.stops(p[3], e)

    def path(self, segs):
        cmds, ops = [], b""
        px, py = self.pen
        self.fx = self.fy = 0.0                          # the current point unrounded (the half-arc test)
        fsx = fsy = 0.0
        sx, sy = px, py
        for s in segs:
            op = s[0]
            if op != "Z":
                nfx, nfy = s[-2], s[-1]
            else:
                nfx, nfy = fsx, fsy
            if op == "M":
                fsx, fsy = s[1], s[2]
                x, y = self.q(s[1]), self.q(s[2])
                cmds.append(6)
                ops += zvar(x - px) + zvar(y - py)
                px, py = sx, sy = x, y
            elif op == "L":
                x, y = self.q(s[1]), self.q(s[2])
                if x == px and y == py:
                    continue
                if y == py:
                    cmds.append(1)
                    ops += zvar(x - px)
                elif x == px:
                    cmds.append(2)
                    ops += zvar(y - py)
                else:
                    cmds.append(0)
                    ops += zvar(x - px) + zvar(y - py)
                px, py = x, y
            elif op in ("C", "Q"):
                cmds.append(3 if op == "C" else 4)
                for i in range(1, len(s), 2):
                    x, y = self.q(s[i]), self.q(s[i + 1])
                    ops += zvar(x - px) + zvar(y - py)
                    px, py = x, y
            elif op == "A":
                rx, ry, rot, large, sweep, ex, ey = s[1:]
                x, y = self.q(ex), self.q(ey)
                R, RY = self.q(rx), self.q(ry)
                was_half = rx > 0 and ry > 0 and arc_lambda(self.fx, self.fy, ex, ey, rx, ry, rot) >= 1.0 - 1e-6
                R, RY = half_arc_fix(px, py, x, y, R, RY, rot, was_half)
                ell = R != RY
                rk = (R, RY, int(round(rot)) % 360 if ell else 0)
                same = rk == self.last_r
                cmds.append(5 | ((1 if large else 0) | (2 if sweep else 0) | (4 if same else 0)) << 3)
                if not same:
                    ops += varuint(R << 1 | (1 if ell else 0))
                    if ell:
                        ops += varuint(RY) + zvar(rk[2])
                self.last_r = rk
                ops += zvar(x - px) + zvar(y - py)
                px, py = x, y
            elif op == "Z":
                cmds.append(7)
                px, py = sx, sy
            self.fx, self.fy = nfx, nfy
        self.pen = (px, py)
        acc = nbits = 0
        for c in cmds:                                   # 3 bits a command, an arc 3 more (large, sweep, same r)
            acc |= c << nbits
            nbits += 6 if (c & 7) == 5 else 3
        packed = acc.to_bytes((nbits + 7) // 8, "little") if nbits else b""
        return varuint(len(cmds)) + packed + ops

    def shape(self, s):
        fk, fp = self.paint(s["fill"])
        sk, sp = self.paint(s["stroke"])
        if not fk and not sk:
            return b""
        out = b""
        if sk and s.get("dash"):
            v, off = s["dash"]
            if self.maxver < 2:
                self.doc.dropped.append((s.get("tag", "shape"), "version 1: dashes drawn solid"))
            else:
                self.v2 = True
                out += bytes([0x42, len(v)]) + b"".join(varuint(max(0, self.q(x))) for x in v) + zvar(self.q(off))
        same = self.last is not None and s["segs"] == self.last
        kind = fk | sk << 2 | (16 if s["rule"] == "evenodd" else 0) | (32 if same else 0)
        out += bytes([kind]) + fp + sp
        if sk:
            ml = max(0, min(15, int(round((s["miter"] - 1) * 2))))
            out += varuint(max(0, self.q(s["width"]))) + bytes([CAPS.get(s["cap"], 0) | JOINS.get(s["join"], 0) << 2 | ml << 4])
        if not same:
            out += self.path(simplify(s["segs"], self.tol))
        self.last = s["segs"]
        return out

    def group(self, g):
        """a version 2 group: its header (the clip path last in it), its body, its end"""
        self.v2 = True
        fl = 0
        head = b""
        b = svgfront.group_box(g) or (0.0, 0.0, 0.0, 0.0)
        x0, y0 = int(math.floor(b[0] * self.k)), int(math.floor(b[1] * self.k))
        x1, y1 = int(math.ceil(b[2] * self.k)), int(math.ceil(b[3] * self.k))
        if g["opacity"] < 0.999:
            fl |= 1
            head += bytes([max(0, min(255, int(round(g["opacity"] * 255))))])
        if g["shadows"]:
            fl |= 8
            head += bytes([len(g["shadows"])])
            for dx, dy, sg, c in g["shadows"]:
                head += zvar(self.q(dx)) + zvar(self.q(dy)) + varuint(max(0, self.q(sg))) + self.color(c)
        if g["blur"] > 0:
            fl |= 16
            head += varuint(max(1, self.q(g["blur"])))
        clip = b""
        if g["clip"] is not None:
            fl |= 2 | (4 if g["clip"]["rule"] == "evenodd" else 0)
            clip = self.path(simplify(g["clip"]["segs"], self.tol))
        self.last = None                                 # the context's path is not this file's last any more
        body = self.items(g["items"]) + b"\x41"
        self.last = None
        return (bytes([0x40, fl]) + varuint(len(body)) + zvar(x0) + zvar(y0) + varuint(max(0, x1 - x0)) +
                varuint(max(0, y1 - y0)) + head + clip + body)

    def items(self, items):
        out = b""
        for it in items:
            out += self.group(it) if "group" in it else self.shape(it)
        return out

    def encode(self):
        items = self.doc.shapes
        if self.maxver < 2:
            items = flatten_v1(self.doc, items)
        body = self.items(items)
        alpha = any(c[3] != 255 for c in self.colors)
        head = b"mV" + bytes([2 if self.v2 else 1, self.F | (64 if alpha else 0)])
        head += varuint(int(math.ceil(self.doc.vb[2]))) + varuint(int(math.ceil(self.doc.vb[3])))
        head += varuint(len(self.colors)) + b"".join(bytes(c if alpha else c[:3]) for c in self.colors)
        return head + body + b"\x00"


def flatten_v1(doc, items, k=1.0):
    """the tree as version 1 can draw it: groups opened, their opacity multiplied into each shape (approximately),
    clips, shadows and blurs dropped - each reported"""
    out = []
    for it in items:
        if "group" in it:
            for what, on in (("clip", it["clip"] is not None), ("shadows", bool(it["shadows"])), ("blur", it["blur"] > 0)):
                if on:
                    doc.dropped.append(("g", f"version 1: {what} dropped"))
            if it["opacity"] < 0.999:
                doc.dropped.append(("g", "version 1: the group's opacity multiplied into each shape"))
            out += flatten_v1(doc, it["items"], k * it["opacity"])
        else:
            if k < 0.999:
                it = dict(it, fill=svgfront.scale_alpha(it["fill"], k), stroke=svgfront.scale_alpha(it["stroke"], k))
            out.append(it)
    return out


def encode(doc, px=240, prec=0.25, quality=None, maxver=VERSION):
    """quality 1..100 (100: a grid step of 0.25 px at px, invisible) overrides prec when given; maxver 1: a picture
    every version 1 decoder draws (version 2's features approximated or dropped, and reported)"""
    return Enc(doc, px, prec, quality, maxver).encode()


# ---- decoder (the reference the C decoder is checked against) --------------------------------------------------------

class Rd:
    def __init__(self, b):
        self.b, self.i = b, 0

    def u8(self):
        self.i += 1
        return self.b[self.i - 1]

    def vu(self):
        v = s = 0
        while True:
            c = self.u8()
            v |= (c & 0x7F) << s
            s += 7
            if not c & 0x80:
                return v

    def zv(self):
        b = self.u8()
        if b < 240:
            return b - 120
        if b < 255:
            i = (b - 240) << 8 | self.u8()
            return 120 + i if i < 1920 else -121 - (i - 1920)
        v = self.vu()
        return (v >> 1) if not v & 1 else -((v + 1) >> 1)


def decode(b):
    """-> svgfront.Doc with the shapes (coordinates in design units)"""
    r = Rd(b)
    assert r.u8() == ord("m") and r.u8() == ord("V"), "not mcuVG"
    assert 1 <= r.u8() <= VERSION, "a version this decoder does not read"
    fl = r.u8()
    F, alpha = fl & 15, fl & 64
    assert not fl & 0xB0, "reserved bits"
    k = float(1 << F)
    d = svgfront.Doc()
    w, h = r.vu(), r.vu()
    d.w, d.h, d.vb = w, h, (0, 0, w, h)
    cols = []
    for _ in range(r.vu()):
        c = tuple(r.u8() for _ in range(4 if alpha else 3))
        cols.append(c if alpha else c + (255,))
    ver = b[2]
    st = {"pen": [0, 0], "last": None, "lastr": (0, 0, 0)}

    def stops(kind):
        n = r.u8()
        ext = ("pad", None, 0.0, None, 0.0)
        if n & 0x40:
            assert ver >= 2, "a version 2 gradient in a version 1 file"
            x = r.u8()
            assert not x & 0xF0 and x & 3 != 3, "a gradient's extension"
            spread, focal, fr, ry, rot = SPREAD_N[x & 3], None, 0.0, None, 0.0
            if x & 4:
                assert kind == 3, "a focal point on a linear gradient"
                focal = (r.zv() / k, r.zv() / k)
                fr = r.vu() / k
            if x & 8:
                assert kind == 3, "an ellipse on a linear gradient"
                ry = r.vu() / k
                rot = float(r.zv())
            ext = (spread, focal, fr, ry, rot)
            n &= ~0x40
        if n & 0x80:
            assert n & 0x7F == 2, "two stops"
            return [(0.0, cols[r.vu()]), (1.0, cols[r.vu()])], ext
        out = []
        for _ in range(n):
            o = r.u8() / 255.0
            out.append((o, cols[r.vu()]))
        return out, ext

    def paint(kind):
        if kind == 0:
            return None
        if kind == 1:
            return ("flat", cols[r.vu()])
        if kind == 2:
            x0, y0 = r.zv(), r.zv()
            dx, dy = r.zv(), r.zv()
            sts, ext = stops(2)
            return ("linear", (x0 / k, y0 / k), ((x0 + dx) / k, (y0 + dy) / k), sts, ext[0])
        cx, cy = r.zv(), r.zv()
        rr = r.vu()
        sts, (spread, focal, fr, ry, rot) = stops(3)
        C = (cx / k, cy / k)
        return ("radial", C, rr / k, sts, focal if focal is not None else C, spread, fr,
                ry if ry is not None else rr / k, rot)

    def path():
        n = r.vu()
        cmds, bit = [], 0
        start = r.i

        def bits(nb):
            nonlocal bit
            v = 0
            for j in range(nb):
                v |= ((b[start + (bit + j) // 8] >> ((bit + j) % 8)) & 1) << j
            bit += nb
            return v
        for i in range(n):
            c = bits(3)
            cmds.append(c | bits(3) << 3 if c == 5 else c)
        r.i = start + (bit + 7) // 8
        segs = []
        px, py = st["pen"]
        sx, sy = px, py
        for c in cmds:
            if c in (0, 6):
                px += r.zv()
                py += r.zv()
                segs.append(("L" if c == 0 else "M", px / k, py / k))
                if c == 6:
                    sx, sy = px, py
            elif c == 1:
                px += r.zv()
                segs.append(("L", px / k, py / k))
            elif c == 2:
                py += r.zv()
                segs.append(("L", px / k, py / k))
            elif c in (3, 4):
                pts = []
                for _ in range(3 if c == 3 else 2):
                    px += r.zv()
                    py += r.zv()
                    pts += [px / k, py / k]
                segs.append(("C" if c == 3 else "Q",) + tuple(pts))
            elif c & 7 == 5:
                f = c >> 3
                if not f & 4:
                    v = r.vu()
                    rx, ry, rot = v >> 1, v >> 1, 0
                    if v & 1:
                        ry = r.vu()
                        rot = r.zv()
                    st["lastr"] = (rx, ry, rot)
                rx, ry, rot = st["lastr"]
                px += r.zv()
                py += r.zv()
                segs.append(("A", rx / k, ry / k, float(rot), f & 1, (f >> 1) & 1, px / k, py / k))
            elif c == 7:
                segs.append(("Z",))
                px, py = sx, sy
        st["pen"] = [px, py]
        return segs

    def body(out, in_group):
        dash = None
        while True:
            kind = r.u8()
            if kind == 0:
                assert not in_group, "a group not ended"
                return
            assert not kind & 0x80, "a reserved kind"
            if kind & 0x40:
                assert ver >= 2, "a version 2 record in a version 1 file"
                if kind == 0x41:
                    assert in_group, "a group's end outside a group"
                    return
                if kind == 0x42:
                    n = r.u8()
                    assert 1 <= n <= 8, "dashes"
                    dash = ([r.vu() / k for _ in range(n)], r.zv() / k)
                    continue
                assert kind == 0x40, "an unknown record"
                fl = r.u8()
                assert not fl & 0xE0, "a group's flags"
                blen = r.vu()
                gx, gy, gw, gh = r.zv(), r.zv(), r.vu(), r.vu()
                g = {"group": True, "opacity": 1.0, "clip": None, "shadows": [], "blur": 0.0, "items": [], "tag": "g",
                     "bbox": (gx / k, gy / k, (gx + gw) / k, (gy + gh) / k)}
                if fl & 1:
                    g["opacity"] = r.u8() / 255.0
                if fl & 8:
                    for _ in range(r.u8()):
                        g["shadows"].append((r.zv() / k, r.zv() / k, r.vu() / k, cols[r.vu()]))
                if fl & 16:
                    g["blur"] = r.vu() / k
                if fl & 2:
                    g["clip"] = {"segs": path(), "rule": "evenodd" if fl & 4 else "nonzero"}
                start = r.i
                st["last"] = None
                body(g["items"], True)
                assert r.i == start + blen, "a group's length"
                st["last"] = None
                out.append(g)
                continue
            fill = paint(kind & 3)
            stroke = paint(kind >> 2 & 3)
            width, cap, join, miter = 0.0, "butt", "miter", 4.0
            if stroke:
                width = r.vu() / k
                sb = r.u8()
                cap, join, miter = CAP_N.get(sb & 3, "butt"), JOIN_N.get(sb >> 2 & 3, "miter"), 1 + (sb >> 4) / 2
            assert not (kind & 32 and st["last"] is None), "the previous path taken where there is none"
            segs = st["last"] if kind & 32 else path()
            st["last"] = segs
            sh = {"segs": segs, "fill": fill, "stroke": stroke, "width": width, "cap": cap, "join": join,
                  "miter": miter, "rule": "evenodd" if kind & 16 else "nonzero", "tag": "mcuvg"}
            if dash and stroke:
                sh["dash"] = dash
            dash = None
            out.append(sh)

    body(d.shapes, False)
    return d


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "enc":
        px = float(a[a.index("--px") + 1]) if "--px" in a else 240
        prec = float(a[a.index("--prec") + 1]) if "--prec" in a else 0.25
        doc = svgfront.load(a[1])
        b = encode(doc, px, prec)
        open(a[2], "wb").write(b)
        print(f"{len(b)} B, grid 1/{1 << Enc(doc, px, prec).F}, {len(doc.shapes)} shapes, dropped {len(doc.dropped)}")
    elif len(a) == 2 and a[0] == "dump":
        d = decode(open(a[1], "rb").read())
        print(f"{d.w} x {d.h}, version {open(a[1], 'rb').read()[2]}, {len(d.shapes)} items")

        def show(items, ind):
            for s in items:
                if "group" in s:
                    print(ind + f"group: opacity {s['opacity']:.2f}, clip {'yes' if s['clip'] else 'no'}, "
                          f"{len(s['shadows'])} shadows, blur {s['blur']:g}, box {tuple(round(v, 2) for v in s['bbox'])}")
                    show(s["items"], ind + "  ")
                else:
                    print(ind, s["fill"] and s["fill"][0], s["stroke"] and s["stroke"][0], s["rule"],
                          "".join(x[0] for x in s["segs"]) + (f" dash {s['dash'][0]}" if s.get("dash") else ""))
        show(d.shapes, " ")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
