#!/usr/bin/env python3
"""mcuVG - the SVG front end of the mcuVG encoder: an SVG file -> plain shapes in the picture's own units.

    python3 tools/svgfront.py FILE.svg        # what the file turns into, shape by shape, and what was dropped

SVG stays the authors' format (designers and AIs write it); the device never parses it.  This module reads the subset that
icons and flat art use and hands the encoder shapes with nothing left to resolve:

  - every element as one path in ABSOLUTE coordinates of the viewBox, transforms applied (rect, circle, ellipse, line,
    polyline, polygon become paths; H / V become lines; S / T become explicit curves);
  - an arc stays an arc while the transform keeps circles circles (translate, rotate, uniform scale), else it becomes
    cubic curves;
  - paint resolved: flat colour (RGBA 0..255) or a gradient in user space (objectBoundingBox and gradientTransform
    folded in, href chains followed); currentColor, inheritance, presentation attributes, style="", <style> rules for
    .class and tag selectors;
  - group opacity multiplied into each shape's alpha (an approximation: overlapping shapes of one group see each other
    through it - the rasteriser has no layer for a true group opacity).

  - text and markers turned into ordinary paths here (the format and the device need neither): <text> and <tspan>
    through the glyph outlines of a TrueType font found by its family (tools/ttf.py), markers (marker-start / -mid /
    -end) placed at the path's vertices with their orientation and units; each is reported as converted.

What it cannot express is reported, never silently drawn wrong: mask, clipPath, filter, image, pattern, dashes,
gradient spread other than pad.  The encoder decides what to do with each report (refuse, or drop and warn).
"""
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"

NAMED = {                  # the CSS colour keywords (148), and transparent
    "aliceblue": (240, 248, 255), "antiquewhite": (250, 235, 215), "aqua": (0, 255, 255),
    "aquamarine": (127, 255, 212), "azure": (240, 255, 255), "beige": (245, 245, 220), "bisque": (255, 228, 196),
    "black": (0, 0, 0), "blanchedalmond": (255, 235, 205), "blue": (0, 0, 255), "blueviolet": (138, 43, 226),
    "brown": (165, 42, 42), "burlywood": (222, 184, 135), "cadetblue": (95, 158, 160), "chartreuse": (127, 255, 0),
    "chocolate": (210, 105, 30), "coral": (255, 127, 80), "cornflowerblue": (100, 149, 237),
    "cornsilk": (255, 248, 220), "crimson": (220, 20, 60), "cyan": (0, 255, 255), "darkblue": (0, 0, 139),
    "darkcyan": (0, 139, 139), "darkgoldenrod": (184, 134, 11), "darkgray": (169, 169, 169),
    "darkgreen": (0, 100, 0), "darkgrey": (169, 169, 169), "darkkhaki": (189, 183, 107),
    "darkmagenta": (139, 0, 139), "darkolivegreen": (85, 107, 47), "darkorange": (255, 140, 0),
    "darkorchid": (153, 50, 204), "darkred": (139, 0, 0), "darksalmon": (233, 150, 122),
    "darkseagreen": (143, 188, 143), "darkslateblue": (72, 61, 139), "darkslategray": (47, 79, 79),
    "darkslategrey": (47, 79, 79), "darkturquoise": (0, 206, 209), "darkviolet": (148, 0, 211),
    "deeppink": (255, 20, 147), "deepskyblue": (0, 191, 255), "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105),
    "dodgerblue": (30, 144, 255), "firebrick": (178, 34, 34), "floralwhite": (255, 250, 240),
    "forestgreen": (34, 139, 34), "fuchsia": (255, 0, 255), "gainsboro": (220, 220, 220),
    "ghostwhite": (248, 248, 255), "gold": (255, 215, 0), "goldenrod": (218, 165, 32), "gray": (128, 128, 128),
    "green": (0, 128, 0), "greenyellow": (173, 255, 47), "grey": (128, 128, 128), "honeydew": (240, 255, 240),
    "hotpink": (255, 105, 180), "indianred": (205, 92, 92), "indigo": (75, 0, 130), "ivory": (255, 255, 240),
    "khaki": (240, 230, 140), "lavender": (230, 230, 250), "lavenderblush": (255, 240, 245),
    "lawngreen": (124, 252, 0), "lemonchiffon": (255, 250, 205), "lightblue": (173, 216, 230),
    "lightcoral": (240, 128, 128), "lightcyan": (224, 255, 255), "lightgoldenrodyellow": (250, 250, 210),
    "lightgray": (211, 211, 211), "lightgreen": (144, 238, 144), "lightgrey": (211, 211, 211),
    "lightpink": (255, 182, 193), "lightsalmon": (255, 160, 122), "lightseagreen": (32, 178, 170),
    "lightskyblue": (135, 206, 250), "lightslategray": (119, 136, 153), "lightslategrey": (119, 136, 153),
    "lightsteelblue": (176, 196, 222), "lightyellow": (255, 255, 224), "lime": (0, 255, 0),
    "limegreen": (50, 205, 50), "linen": (250, 240, 230), "magenta": (255, 0, 255), "maroon": (128, 0, 0),
    "mediumaquamarine": (102, 205, 170), "mediumblue": (0, 0, 205), "mediumorchid": (186, 85, 211),
    "mediumpurple": (147, 112, 219), "mediumseagreen": (60, 179, 113), "mediumslateblue": (123, 104, 238),
    "mediumspringgreen": (0, 250, 154), "mediumturquoise": (72, 209, 204), "mediumvioletred": (199, 21, 133),
    "midnightblue": (25, 25, 112), "mintcream": (245, 255, 250), "mistyrose": (255, 228, 225),
    "moccasin": (255, 228, 181), "navajowhite": (255, 222, 173), "navy": (0, 0, 128), "oldlace": (253, 245, 230),
    "olive": (128, 128, 0), "olivedrab": (107, 142, 35), "orange": (255, 165, 0), "orangered": (255, 69, 0),
    "orchid": (218, 112, 214), "palegoldenrod": (238, 232, 170), "palegreen": (152, 251, 152),
    "paleturquoise": (175, 238, 238), "palevioletred": (219, 112, 147), "papayawhip": (255, 239, 213),
    "peachpuff": (255, 218, 185), "peru": (205, 133, 63), "pink": (255, 192, 203), "plum": (221, 160, 221),
    "powderblue": (176, 224, 230), "purple": (128, 0, 128), "rebeccapurple": (102, 51, 153), "red": (255, 0, 0),
    "rosybrown": (188, 143, 143), "royalblue": (65, 105, 225), "saddlebrown": (139, 69, 19),
    "salmon": (250, 128, 114), "sandybrown": (244, 164, 96), "seagreen": (46, 139, 87), "seashell": (255, 245, 238),
    "sienna": (160, 82, 45), "silver": (192, 192, 192), "skyblue": (135, 206, 235), "slateblue": (106, 90, 205),
    "slategray": (112, 128, 144), "slategrey": (112, 128, 144), "snow": (255, 250, 250),
    "springgreen": (0, 255, 127), "steelblue": (70, 130, 180), "tan": (210, 180, 140), "teal": (0, 128, 128),
    "thistle": (216, 191, 216), "tomato": (255, 99, 71), "turquoise": (64, 224, 208), "violet": (238, 130, 238),
    "wheat": (245, 222, 179), "white": (255, 255, 255), "whitesmoke": (245, 245, 245), "yellow": (255, 255, 0),
    "yellowgreen": (154, 205, 50),
    "transparent": None,
}

INHERITED = ("fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "fill-rule",
             "fill-opacity", "stroke-opacity", "color", "stroke-dasharray", "stroke-dashoffset", "visibility", "display",
             "font-family", "font-size", "font-weight", "font-style", "text-anchor", "letter-spacing",
             "marker-start", "marker-mid", "marker-end")
NOT_INHERITED = ("opacity", "transform", "stop-color", "stop-opacity", "clip-path", "mask", "filter")

# ---- numbers and matrices ------------------------------------------------------------------------------------------

NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def nums(s):
    return [float(v) for v in NUM.findall(s or "")]


def length(s, ref=1.0, default=0.0):
    """a length or a percentage of ref; units other than px/% are not expected in icons and are taken as user units"""
    if s is None:
        return default
    s = s.strip()
    if s.endswith("%"):
        return float(s[:-1]) / 100.0 * ref
    m = NUM.match(s)
    return float(m.group(0)) if m else default


def mat_mul(a, b):
    """a * b for (a, b, c, d, e, f): apply b first, then a"""
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
            a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def parse_transform(s):
    m = ID
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = nums(args)
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            a = math.radians(v[0])
            t = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                t = mat_mul(mat_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX":
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY":
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = mat_mul(m, t)
    return m


def similarity(m):
    """(scale, angle in degrees, mirrored) when m keeps circles circles, else None"""
    a, b, c, d = m[:4]
    sx, sy = math.hypot(a, b), math.hypot(c, d)
    if sx == 0 or sy == 0 or abs(sx - sy) > 1e-6 * max(sx, sy) or abs(a * c + b * d) > 1e-6 * sx * sy:
        return None
    return sx, math.degrees(math.atan2(b, a)), (a * d - b * c) < 0


# ---- colours -------------------------------------------------------------------------------------------------------

def parse_color(s, current=None):
    """-> (r, g, b, a) 0..255, None for 'none', or ('url', id)"""
    if s is None:
        return None
    s = s.strip()
    if s in ("none", ""):
        return None
    if s.startswith("url("):
        m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", s)
        return ("url", m.group(1)) if m else None
    if s == "currentColor":
        return parse_color(current or "black")
    if s.startswith("#"):
        h = s[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        if len(h) == 6:
            h += "ff"
        v = int(h, 16)
        return (v >> 24 & 255, v >> 16 & 255, v >> 8 & 255, v & 255)
    m = re.match(r"rgba?\(([^)]*)\)", s)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", ",").split(",") if p.strip()]
        ch = [round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p)) for p in parts[:3]]
        a = 255
        if len(parts) > 3:
            p = parts[3]
            a = round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p) * 255)
        return tuple(max(0, min(255, v)) for v in ch) + (max(0, min(255, a)),)
    m = re.match(r"hsla?\(([^)]*)\)", s)
    if m:                                       # hsl(h s% l%) and hsla(): CSS Color 3/4, commas or spaces
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        h = float(parts[0].replace("deg", "")) % 360 / 360
        sat, lig = (float(v.rstrip("%")) / 100 for v in parts[1:3])
        a = 255
        if len(parts) > 3:
            p = parts[3]
            a = round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p) * 255)
        import colorsys
        r, g, b = colorsys.hls_to_rgb(h, lig, sat)
        return tuple(max(0, min(255, round(v * 255))) for v in (r, g, b)) + (max(0, min(255, a)),)
    if s.lower() not in NAMED:
        raise ValueError(f"colour {s!r} not understood")
    rgb = NAMED[s.lower()]
    return None if rgb is None else rgb + (255,)


# ---- paths ---------------------------------------------------------------------------------------------------------

NUMRE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


class Scan:
    """path data cursor: numbers, and the arc's flags as single characters (they may be glued: 'a8 8 0 0110 10')"""
    def __init__(self, d):
        self.d, self.i = d, 0

    def skip(self):
        while self.i < len(self.d) and self.d[self.i] in " \t\r\n,":
            self.i += 1

    def command(self):
        self.skip()
        if self.i < len(self.d) and self.d[self.i] in "MmLlHhVvCcSsQqTtAaZz":
            self.i += 1
            return self.d[self.i - 1]
        return None

    def more(self):
        self.skip()
        return self.i < len(self.d) and self.d[self.i] not in "MmLlHhVvCcSsQqTtAaZz"

    def number(self):
        self.skip()
        m = NUMRE.match(self.d, self.i)
        if not m:
            raise ValueError(f"path data: number expected at {self.i}: {self.d[self.i:self.i + 12]!r}")
        self.i = m.end()
        return float(m.group(0))

    def flag(self):
        self.skip()
        if self.i < len(self.d) and self.d[self.i] in "01":
            self.i += 1
            return int(self.d[self.i - 1])
        raise ValueError(f"path data: arc flag expected at {self.i}")


def parse_path(d):
    """SVG path data -> absolute segments: ('M', x, y) ('L', x, y) ('Q', x1, y1, x, y) ('C', x1, y1, x2, y2, x, y)
    ('A', rx, ry, rot, large, sweep, x, y) ('Z',)"""
    sc = Scan(d)
    segs = []
    cx = cy = sx = sy = 0.0
    last_c = last_q = None
    cmd = None
    while True:
        c = sc.command()
        if c is None:
            if not sc.more():
                break
            if cmd is None:
                raise ValueError("path data starts without a command")
            c = cmd                                         # an implicit repeat of the last command
        cmd = c
        C, rel = c.upper(), c.islower()
        if C == "Z":
            segs.append(("Z",))
            cx, cy = sx, sy
            last_c = last_q = None
            continue
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        if C == "A":
            a = [sc.number(), sc.number(), sc.number(), sc.flag(), sc.flag(), sc.number(), sc.number()]
        else:
            a = [sc.number() for _ in range(ARGS[C])]
        nc = nq = None
        if C == "M":
            cx, cy = ox + a[0], oy + a[1]
            sx, sy = cx, cy
            segs.append(("M", cx, cy))
            cmd = "l" if rel else "L"                      # further pairs are implicit lines
        elif C == "L":
            cx, cy = ox + a[0], oy + a[1]
            segs.append(("L", cx, cy))
        elif C == "H":
            cx = (cx if rel else 0.0) + a[0]
            segs.append(("L", cx, cy))
        elif C == "V":
            cy = (cy if rel else 0.0) + a[0]
            segs.append(("L", cx, cy))
        elif C in "CS":
            if C == "C":
                x1, y1, x2, y2, x, y = ox + a[0], oy + a[1], ox + a[2], oy + a[3], ox + a[4], oy + a[5]
            else:
                x1, y1 = (2 * cx - last_c[0], 2 * cy - last_c[1]) if last_c else (cx, cy)
                x2, y2, x, y = ox + a[0], oy + a[1], ox + a[2], oy + a[3]
            segs.append(("C", x1, y1, x2, y2, x, y))
            nc = (x2, y2)
            cx, cy = x, y
        elif C in "QT":
            if C == "Q":
                x1, y1, x, y = ox + a[0], oy + a[1], ox + a[2], oy + a[3]
            else:
                x1, y1 = (2 * cx - last_q[0], 2 * cy - last_q[1]) if last_q else (cx, cy)
                x, y = ox + a[0], oy + a[1]
            segs.append(("Q", x1, y1, x, y))
            nq = (x1, y1)
            cx, cy = x, y
        elif C == "A":
            x, y = ox + a[5], oy + a[6]
            segs.append(("A", abs(a[0]), abs(a[1]), a[2], a[3], a[4], x, y))
            cx, cy = x, y
        last_c, last_q = nc, nq
    return segs


def arc_to_cubics(x0, y0, rx, ry, rot, large, sweep, x, y):
    """SVG arc (endpoint form) -> list of ('C', ...) pieces of at most 90 degrees; a degenerate arc is a line"""
    if rx == 0 or ry == 0 or (x0 == x and y0 == y):
        return [("L", x, y)]
    phi = math.radians(rot)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x) / 2, (y0 - y) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x0 + x) / 2
    cy = sp * cxp + cp * cyp + (y0 + y) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(1, int(math.ceil(abs(dt) / (math.pi / 2) - 1e-9)))
    step = dt / n
    k = 4 / 3 * math.tan(step / 4)
    out = []

    def pt(t):
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        return cp * ex - sp * ey + cx, sp * ex + cp * ey + cy

    def der(t):
        ex, ey = -rx * math.sin(t), ry * math.cos(t)
        return cp * ex - sp * ey, sp * ex + cp * ey

    t = t1
    for j in range(n):
        p0, p3 = pt(t), pt(t + step)
        d0, d3 = der(t), der(t + step)
        c1 = (p0[0] + k * d0[0], p0[1] + k * d0[1])
        c2 = (p3[0] - k * d3[0], p3[1] - k * d3[1])
        if j == n - 1:
            p3 = (x, y)
        out.append(("C", c1[0], c1[1], c2[0], c2[1], p3[0], p3[1]))
        t += step
    return out


def transform_segs(segs, m):
    """apply m; arcs stay arcs under a similarity, become cubics otherwise"""
    if m == ID:
        return list(segs)
    sim = similarity(m)
    out = []
    cx = cy = sx = sy = 0.0
    for s in segs:
        op = s[0]
        if op == "Z":
            out.append(s)
            cx, cy = sx, sy
            continue
        if op == "A":
            rx, ry, rot, large, sweep, x, y = s[1:]
            if sim:
                k, ang, mir = sim
                X, Y = mat_apply(m, x, y)
                out.append(("A", rx * k, ry * k, (rot + ang) if not mir else (ang - rot), large,
                            sweep if not mir else 1 - sweep, X, Y))
            else:
                for c in arc_to_cubics(cx, cy, rx, ry, rot, large, sweep, x, y):
                    out.append(tuple([c[0]] + [v for i in range(1, len(c), 2) for v in mat_apply(m, c[i], c[i + 1])]))
            cx, cy = x, y
            continue
        pts = s[1:]
        out.append(tuple([op] + [v for i in range(0, len(pts), 2) for v in mat_apply(m, pts[i], pts[i + 1])]))
        cx, cy = pts[-2], pts[-1]
        if op == "M":
            sx, sy = cx, cy
    return out


def segs_bbox(segs):
    """bounding box of the outline (control points of curves count: good enough for objectBoundingBox of icons; arcs
    are measured through their cubic pieces)"""
    xs, ys = [], []
    cx = cy = sx = sy = 0.0
    for s in segs:
        if s[0] == "Z":
            cx, cy = sx, sy
            continue
        if s[0] == "A":
            for c in arc_to_cubics(cx, cy, *s[1:]):
                xs += [c[i] for i in range(1, len(c), 2)]
                ys += [c[i] for i in range(2, len(c), 2)]
            cx, cy = s[-2], s[-1]
            continue
        xs += [s[i] for i in range(1, len(s), 2)]
        ys += [s[i] for i in range(2, len(s), 2)]
        cx, cy = s[-2], s[-1]
        if s[0] == "M":
            sx, sy = cx, cy
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def shape_segs(el, tag, st):
    """the element's outline as path segments in its own coordinates; None when it draws nothing"""
    g = el.get
    if tag == "path":
        return parse_path(g("d") or "")
    if tag == "rect":
        x, y = length(g("x")), length(g("y"))
        w, h = length(g("width")), length(g("height"))
        if w <= 0 or h <= 0:
            return None
        rx, ry = g("rx"), g("ry")
        rx = length(rx) if rx is not None else None
        ry = length(ry) if ry is not None else None
        if rx is None and ry is None:
            rx = ry = 0.0
        elif rx is None:
            rx = ry
        elif ry is None:
            ry = rx
        rx, ry = min(rx, w / 2), min(ry, h / 2)
        if rx <= 0 or ry <= 0:
            return [("M", x, y), ("L", x + w, y), ("L", x + w, y + h), ("L", x, y + h), ("Z",)]
        return [("M", x + rx, y), ("L", x + w - rx, y), ("A", rx, ry, 0, 0, 1, x + w, y + ry),
                ("L", x + w, y + h - ry), ("A", rx, ry, 0, 0, 1, x + w - rx, y + h),
                ("L", x + rx, y + h), ("A", rx, ry, 0, 0, 1, x, y + h - ry),
                ("L", x, y + ry), ("A", rx, ry, 0, 0, 1, x + rx, y), ("Z",)]
    if tag in ("circle", "ellipse"):
        cx, cy = length(g("cx")), length(g("cy"))
        if tag == "circle":
            rx = ry = length(g("r"))
        else:
            rx, ry = length(g("rx")), length(g("ry"))
        if rx <= 0 or ry <= 0:
            return None
        return [("M", cx + rx, cy), ("A", rx, ry, 0, 0, 1, cx - rx, cy), ("A", rx, ry, 0, 0, 1, cx + rx, cy), ("Z",)]
    if tag == "line":
        return [("M", length(g("x1")), length(g("y1"))), ("L", length(g("x2")), length(g("y2")))]
    if tag in ("polyline", "polygon"):
        v = nums(g("points"))
        if len(v) < 4:
            return None
        segs = [("M", v[0], v[1])] + [("L", v[i], v[i + 1]) for i in range(2, len(v) - 1, 2)]
        return segs + [("Z",)] if tag == "polygon" else segs
    return None


# ---- the document --------------------------------------------------------------------------------------------------

class Doc:
    def __init__(self):
        self.w = self.h = 0.0
        self.vb = (0.0, 0.0, 0.0, 0.0)
        self.shapes = []           # dicts: segs, fill, stroke, width, cap, join, miter, rule [, dash]; and groups
        self.stack = [self.shapes]  # where the next shape goes (a group's items while it is open)
        self.dropped = []          # (what, why)
        self.converted = []        # (what, how): text and markers turned into ordinary paths here
        self.root = None
        self.fontfaces = {}        # @font-face: family (lower case) -> a TrueType file next to the SVG


def css_rules(root):
    rules = {}
    for st in root.iter(NS + "style"):
        txt = re.sub(r"/\*.*?\*/", "", st.text or "", flags=re.S)
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", txt):
            if sel.strip().startswith("@"):         # @font-face and other at-rules: not selectors
                continue
            decl = parse_style(body)
            for one in sel.split(","):
                one = one.strip()
                if one:
                    rules.setdefault(one, {}).update(decl)
    return rules


def parse_style(s):
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def own_style(el, tag, rules):
    """presentation attributes < CSS rules (tag, then class) < style="" (the cascade an icon needs)"""
    st = {k: v for k, v in el.attrib.items() if not k.startswith("{")}
    st.update(rules.get(tag, {}))
    for cls in (el.get("class") or "").split():
        st.update(rules.get("." + cls, {}))
        st.update(rules.get(tag + "." + cls, {}))
    st.update(parse_style(el.get("style")))
    return st


def gradient(doc, defs, gid, bbox, m, rules, seen=()):
    """resolve url(#gid) for a shape with bbox (its own coordinates) under the shape's matrix m -> paint tuple:
    ('linear', (x1, y1), (x2, y2), stops) or ('radial', (cx, cy), r, stops, (fx, fy)), stops [(offset, rgba)];
    the points in the picture's coordinates.  None when the gradient cannot be expressed (reported)"""
    el = defs.get(gid)
    if el is None or gid in seen:
        doc.dropped.append(("paint", f"url(#{gid}) not found"))
        return None
    kind = el.tag.replace(NS, "") if isinstance(el.tag, str) else ""
    if kind not in ("linearGradient", "radialGradient"):   # a pattern (or anything else): not a paint of the format
        doc.dropped.append(("paint", f"url(#{gid}) is a {kind or 'non-paint'}: not drawn"))
        return None
    chain = [el]
    while True:
        href = chain[-1].get(XLINK) or chain[-1].get("href")
        if not href or not href.startswith("#") or href[1:] not in defs or len(chain) > 8:
            break
        chain.append(defs[href[1:]])

    def attr(name, default=None):
        for e in chain:
            if e.get(name) is not None:
                return e.get(name)
        return default

    stops_el = next((list(e.iter(NS + "stop")) for e in chain if list(e.iter(NS + "stop"))), [])
    stops = []
    for s in stops_el:
        sst = own_style(s, "stop", rules)
        off = sst.get("offset", "0")
        off = float(off[:-1]) / 100 if off.endswith("%") else float(off)
        c = parse_color(sst.get("stop-color", "black")) or (0, 0, 0, 0)
        a = float(sst.get("stop-opacity", "1"))
        off = max(stops[-1][0] if stops else 0.0, min(1.0, off))
        stops.append((off, c[:3] + (round(c[3] * a),)))
    if not stops:
        return None
    spread = (attr("spreadMethod") or "pad").strip()
    if spread not in ("pad", "reflect", "repeat"):
        spread = "pad"
    units = attr("gradientUnits") or "objectBoundingBox"
    gm = parse_transform(attr("gradientTransform"))
    if units == "objectBoundingBox":
        x0, y0, x1, y1 = bbox
        bw, bh = (x1 - x0) or 1e-9, (y1 - y0) or 1e-9
        um = (bw, 0, 0, bh, x0, y0)
        ref_w = ref_h = 1.0
    else:
        um = ID
        ref_w, ref_h = doc.vb[2], doc.vb[3]
    full = mat_mul(m, mat_mul(um, gm))
    tag = el.tag.replace(NS, "")
    if tag == "linearGradient" or chain[0].tag == NS + "linearGradient":
        p1 = (length(attr("x1"), ref_w, 0.0), length(attr("y1"), ref_h, 0.0))
        p2 = (length(attr("x2"), ref_w, ref_w), length(attr("y2"), ref_h, 0.0))
        # t is linear in the picture's coordinates under any affine map: t = n . u + c0, n = L^-T d / |d|^2 (a skew or a
        # non-uniform scale turns the isolines; mapping the two end points alone would not)
        a, b, cc, d_, e, f_ = full
        det = a * d_ - b * cc
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        dd = dx * dx + dy * dy
        if abs(det) < 1e-12 or dd == 0:
            return ("linear", mat_apply(full, *p1), mat_apply(full, *p2), stops, spread)
        nx, ny = (d_ * dx - b * dy) / det / dd, (-cc * dx + a * dy) / det / dd
        c0 = -(nx * e + ny * f_) - (p1[0] * dx + p1[1] * dy) / dd
        nn = nx * nx + ny * ny
        P0 = (-c0 * nx / nn, -c0 * ny / nn)
        return ("linear", P0, (P0[0] + nx / nn, P0[1] + ny / nn), stops, spread)
    ref = math.hypot(ref_w, ref_h) / math.sqrt(2)
    c = (length(attr("cx"), ref_w, 0.5 * ref_w), length(attr("cy"), ref_h, 0.5 * ref_h))
    r = length(attr("r"), ref, 0.5 * ref)
    f = (length(attr("fx"), ref_w, c[0]), length(attr("fy"), ref_h, c[1]))
    fr = length(attr("fr"), ref, 0.0)
    C = mat_apply(full, *c)
    # the circle through the map is an ellipse: its axes from M M^T, M = r L (the isolines are its scaled copies)
    a, b, cc, d_ = full[0] * r, full[1] * r, full[2] * r, full[3] * r
    A, B, D = a * a + cc * cc, a * b + cc * d_, b * b + d_ * d_
    mid, rad = (A + D) / 2, math.sqrt(((A - D) / 2) ** 2 + B * B)
    rx, ry = math.sqrt(max(mid + rad, 0.0)), math.sqrt(max(mid - rad, 0.0))
    rot = math.degrees(0.5 * math.atan2(2 * B, A - D))
    if abs(rx - ry) <= 1e-6 * max(rx, 1e-9):
        ry, rot = rx, 0.0
    F = mat_apply(full, *f)
    frx = fr / r * rx if r > 0 else 0.0               # the focal radius in the same measure as rx
    return ("radial", C, rx, stops, F, spread, frx, ry, rot)


def paint_ext(p):
    """a gradient's version 2 parts: (spread, focal point or None, focal radius, ry, rot); version 1's when none"""
    if p is None or p[0] == "flat":
        return ("pad", None, 0.0, None, 0.0)
    if p[0] == "linear":
        return (p[4] if len(p) > 4 else "pad", None, 0.0, None, 0.0)
    C, rx = p[1], p[2]
    F = p[4] if len(p) > 4 else C
    spread = p[5] if len(p) > 5 else "pad"
    fr = p[6] if len(p) > 6 else 0.0
    ry = p[7] if len(p) > 7 else rx
    rot = p[8] if len(p) > 8 else 0.0
    focal = F if (math.dist(F, C) > 1e-6 * max(rx, 1e-9) or fr > 0) else None
    return (spread, focal, fr, ry if abs(ry - rx) > 1e-6 * max(rx, 1e-9) or abs(rot) > 1e-9 else None, rot)


def scale_alpha(paint, k):
    if paint is None or k >= 0.999:
        return paint
    if paint[0] == "flat":
        c = paint[1]
        return ("flat", c[:3] + (round(c[3] * k),))
    stops = [(o, c[:3] + (round(c[3] * k),)) for o, c in paint[3]]
    return paint[:3] + (stops,) + paint[4:]


def load(path_or_text):
    """read an SVG file (or its text) -> Doc"""
    text = path_or_text
    if not path_or_text.lstrip().startswith("<"):
        with open(path_or_text, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    root = ET.fromstring(text)
    doc = Doc()
    doc.root = root
    here = os.path.dirname(os.path.abspath(path_or_text)) if text is not path_or_text else os.getcwd()
    for st in root.iter(NS + "style"):           # @font-face { font-family: X; src: url(file.ttf) }: the file's own fonts
        for body in re.findall(r"@font-face\s*\{([^}]*)\}", st.text or ""):
            d = parse_style(body)
            fam = (d.get("font-family") or "").strip().strip("'\"").lower()
            src = re.search(r"url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)", d.get("src") or "")
            if fam and src and src.group(1).lower().endswith((".ttf", ".ttc")):
                f = os.path.normpath(os.path.join(here, src.group(1)))
                if os.path.exists(f):
                    doc.fontfaces[fam] = f
    vb = nums(root.get("viewBox"))
    w = length(root.get("width"), default=vb[2] if len(vb) == 4 else 0)
    h = length(root.get("height"), default=vb[3] if len(vb) == 4 else 0)
    if len(vb) != 4:
        vb = [0.0, 0.0, w or 100.0, h or 100.0]
    doc.vb = tuple(vb)
    doc.w, doc.h = (w or vb[2]), (h or vb[3])
    defs = {e.get("id"): e for e in root.iter() if e.get("id")}
    rules = css_rules(root)
    base = (1, 0, 0, 1, -vb[0], -vb[1])          # the picture's origin at the viewBox's corner
    walk(doc, root, "svg", {"fill": "black", "stroke": "none", "stroke-width": "1"}, base, 1.0, defs, rules, False)
    return doc


SKIP_SUBTREE = ("defs", "style", "title", "desc", "metadata", "linearGradient", "radialGradient", "symbol",
                "clipPath", "mask", "pattern", "marker", "filter")
UNSUPPORTED = ("image", "foreignObject", "switch")


def viewport(el):
    """a nested <svg>'s matrix: its viewBox fitted into (x, y, width, height) by preserveAspectRatio (meet / slice,
    the nine alignments, none). Its content is not clipped to the viewport (no clipping in version 1)"""
    x, y = length(el.get("x")), length(el.get("y"))
    vb = nums(el.get("viewBox"))
    w = length(el.get("width"), 1.0, vb[2] if len(vb) == 4 else 0.0)
    h = length(el.get("height"), 1.0, vb[3] if len(vb) == 4 else 0.0)
    if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0 or w <= 0 or h <= 0:
        return (1.0, 0.0, 0.0, 1.0, x, y)
    sx, sy = w / vb[2], h / vb[3]
    par = (el.get("preserveAspectRatio") or "xMidYMid meet").split()
    if par and par[0] == "defer":
        par = par[1:] or ["xMidYMid"]
    align = par[0] if par else "xMidYMid"
    if align != "none":
        k = max(sx, sy) if len(par) > 1 and par[1] == "slice" else min(sx, sy)
        sx = sy = k
        ax = {"xMin": 0.0, "xMid": 0.5, "xMax": 1.0}.get(align[:4], 0.5)
        ay = {"YMin": 0.0, "YMid": 0.5, "YMax": 1.0}.get(align[4:], 0.5)
        x += (w - vb[2] * sx) * ax
        y += (h - vb[3] * sy) * ay
    return (sx, 0.0, 0.0, sy, x - vb[0] * sx, y - vb[1] * sy)


def walk(doc, el, tag, inh, m, opa, defs, rules, in_use):
    """one element: its style and matrix; a group of version 2 around it when it has an opacity, a clip or a filter"""
    st = own_style(el, tag, rules)
    cur = dict(inh)
    for k in INHERITED:
        if k in st and st[k] != "inherit":
            cur[k] = st[k]
    if cur.get("display") == "none" or st.get("display") == "none":
        return
    m = mat_mul(m, parse_transform(st.get("transform")))
    gop = float(st.get("opacity", "1"))
    if st.get("mask") and st["mask"] != "none":
        doc.dropped.append((tag, f"mask={st['mask']} ignored"))
    clip = clip_of(doc, _url(st.get("clip-path")), m, defs, rules, tag) if st.get("clip-path") not in (None, "none") else None
    filt = filter_of(doc, _url(st.get("filter")), m, defs, rules, tag) if st.get("filter") not in (None, "none") else None
    opened = 0
    if gop < 0.999 or clip:                     # SVG's order: the filter, then the clip, then the opacity - outermost
        open_group(doc, gop if gop < 0.999 else 1.0, clip, [], 0.0)
        opened += 1
    if filt:
        open_group(doc, 1.0, None, filt[0], filt[1])
        opened += 1
    if opened:
        try:
            walk_inner(doc, el, tag, st, cur, m, opa, defs, rules, in_use)
        finally:
            for _ in range(opened):
                close_group(doc)
        return
    walk_inner(doc, el, tag, st, cur, m, opa, defs, rules, in_use)


def walk_inner(doc, el, tag, st, cur, m, opa, defs, rules, in_use):
    if tag in ("svg", "g", "a"):
        if tag == "svg" and el is not doc.root:   # a nested viewport: x, y, width, height and its viewBox
            m = mat_mul(m, viewport(el))
        for ch in el:
            t = ch.tag.replace(NS, "") if isinstance(ch.tag, str) else ""
            if t in SKIP_SUBTREE or not t:
                continue
            if t in UNSUPPORTED:
                doc.dropped.append((t, "not expressible in mcuVG"))
                continue
            walk(doc, ch, t, cur, m, opa, defs, rules, in_use)
        return
    if tag == "use":
        href = el.get(XLINK) or el.get("href") or ""
        ref = defs.get(href[1:]) if href.startswith("#") else None
        if ref is None or in_use:
            doc.dropped.append(("use", f"{href} unresolved"))
            return
        m2 = mat_mul(m, (1, 0, 0, 1, length(el.get("x")), length(el.get("y"))))
        t = ref.tag.replace(NS, "")
        if t == "symbol":
            t = "g"
        walk(doc, ref, t, cur, m2, opa, defs, rules, True)
        return
    if tag == "text":
        text_shapes(doc, el, cur, m, opa, defs, rules)
        return
    segs = shape_segs(el, tag, st)
    if not segs:
        if tag not in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"):
            doc.dropped.append((tag, "unknown element"))
        return
    emit(doc, tag, segs, cur, m, opa, defs, rules)
    if tag in ("path", "line", "polyline", "polygon"):
        markers(doc, segs, cur, m, opa, defs, rules)


def emit(doc, tag, segs, cur, m, opa, defs, rules):
    """one outline in its element's own coordinates -> a shape of the picture: its paints resolved, its stroke width
    taken through the matrix, the matrix applied"""
    if cur.get("visibility") in ("hidden", "collapse"):
        return
    bbox = segs_bbox(segs)
    color = cur.get("color")

    def paint(key, okey):
        try:
            c = parse_color(cur.get(key), color)
        except ValueError as e:                   # reported, not drawn black
            doc.dropped.append((tag, f"{key}: {e}"))
            return None
        if c is None:
            return None
        k = opa * float(cur.get(okey, "1"))
        if c[0] == "url":
            try:
                p = gradient(doc, defs, c[1], bbox, m, rules)
            except ValueError as e:
                doc.dropped.append((tag, f"{key}: {e}"))
                return None
            if p is None:
                return None
            return scale_alpha(p, k)
        return scale_alpha(("flat", c), k)

    fill = paint("fill", "fill-opacity")
    stroke = paint("stroke", "stroke-opacity")
    sim = similarity(m)
    width = length(cur.get("stroke-width"), math.hypot(doc.vb[2], doc.vb[3]) / math.sqrt(2), 1.0)
    if stroke is not None:
        if sim:
            width *= sim[0]
        else:
            k = math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
            width *= k
            doc.dropped.append((tag, "stroke under a non-uniform transform drawn with the mean width"))
    dash = None
    if stroke is not None and cur.get("stroke-dasharray") not in (None, "none", ""):
        diag = math.hypot(doc.vb[2], doc.vb[3]) / math.sqrt(2)
        try:
            v = [length(x, diag, 0.0) for x in re.split(r"[\s,]+", cur["stroke-dasharray"].strip()) if x]
        except ValueError:
            v = []
        if v and min(v) >= 0 and sum(v) > 0:
            if len(v) % 2:
                v = v * 2
            k = sim[0] if sim else math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
            if len(v) <= 8:
                dash = ([x * k for x in v], length(cur.get("stroke-dashoffset"), diag, 0.0) * k)
            else:
                doc.dropped.append((tag, f"stroke-dasharray of {len(v)} lengths (8 at most): drawn solid"))
    if fill is None and (stroke is None or width <= 0):
        return
    shape = {
        "segs": transform_segs(segs, m),
        "fill": fill,
        "stroke": stroke if width > 0 else None,
        "width": width,
        "cap": cur.get("stroke-linecap", "butt"),
        "join": cur.get("stroke-linejoin", "miter"),
        "miter": float(cur.get("stroke-miterlimit", "4")),
        "rule": cur.get("fill-rule", "nonzero"),
        "tag": tag,
    }
    if dash:
        shape["dash"] = dash
    doc.stack[-1].append(shape)



# ---- groups (version 2): exact opacity, clipping, shadows and glows, blur ------------------------------------------------

def open_group(doc, opacity, clip, shadows, blur):
    g = {"group": True, "opacity": opacity, "clip": clip, "shadows": list(shadows), "blur": blur, "items": [],
         "tag": "g"}
    doc.stack[-1].append(g)
    doc.stack.append(g["items"])


def close_group(doc):
    items = doc.stack.pop()
    parent = doc.stack[-1]
    g = parent[-1]
    if not items:                                       # draws nothing
        parent.pop()
        return
    # opacity alone over one shape that only fills or only strokes: exactly its paint's alpha - no layer
    if (g["clip"] is None and not g["shadows"] and not g["blur"] and len(items) == 1 and "group" not in items[0]
            and (items[0]["fill"] is None or items[0]["stroke"] is None)):
        sh = items[0]
        sh["fill"] = scale_alpha(sh["fill"], g["opacity"])
        sh["stroke"] = scale_alpha(sh["stroke"], g["opacity"])
        parent[-1] = sh
        return
    if g["opacity"] >= 0.999 and g["clip"] is None and not g["shadows"] and not g["blur"]:
        parent[-1:] = items                               # an empty group: its items in its place
        return


def item_box(it):
    """what an item draws, at most: (x0, y0, x1, y1) in the picture's units"""
    if "group" in it:
        b = group_box(it)
        if b is None:
            return None
        g = 0.0
        for dx, dy, sg, _c in it["shadows"]:
            g = max(g, abs(dx) + 3 * sg, abs(dy) + 3 * sg)
        g = max(g, 3 * it["blur"])
        return (b[0] - g, b[1] - g, b[2] + g, b[3] + g)
    b = segs_bbox(it["segs"])
    if it["stroke"] is not None:
        hw = it["width"] / 2 * (max(1.0, it["miter"]) if it["join"] == "miter" else 1.0) * (1.42 if it["cap"] == "square" else 1.0)
        b = (b[0] - hw, b[1] - hw, b[2] + hw, b[3] + hw)
    return b


def group_box(g):
    """the box of the group's own drawing (its items, inside its clip) - what its record carries"""
    bs = [b for b in (item_box(it) for it in g["items"]) if b is not None]
    if not bs:
        return None
    b = (min(x[0] for x in bs), min(x[1] for x in bs), max(x[2] for x in bs), max(x[3] for x in bs))
    if g["clip"] is not None:
        c = segs_bbox(g["clip"]["segs"])
        b = (max(b[0], c[0]), max(b[1], c[1]), min(b[2], c[2]), min(b[3], c[3]))
        if b[0] >= b[2] or b[1] >= b[3]:
            return (b[0], b[1], b[0], b[1])
    return b


def flat_items(items):
    """every shape of a tree, groups opened (what a version 1 picture of it draws)"""
    for it in items:
        if "group" in it:
            yield from flat_items(it["items"])
        else:
            yield it


def clip_of(doc, cid, m, defs, rules, tag):
    """clip-path=url(#cid) -> {"segs", "rule"} in the picture's coordinates, or None (reported)"""
    el = defs.get(cid) if cid else None
    if el is None or el.tag.replace(NS, "") != "clipPath":
        doc.dropped.append((tag, f"clip-path #{cid} not found"))
        return None
    if (el.get("clipPathUnits") or "userSpaceOnUse") != "userSpaceOnUse":
        doc.dropped.append((tag, f"clip-path #{cid}: clipPathUnits objectBoundingBox not supported - not clipped"))
        return None
    cm = mat_mul(m, parse_transform(el.get("transform")))
    segs, rule = [], None
    for ch in el:
        t = ch.tag.replace(NS, "") if isinstance(ch.tag, str) else ""
        if not t:
            continue
        if t == "use":
            href = ch.get(XLINK) or ch.get("href") or ""
            ref = defs.get(href[1:]) if href.startswith("#") else None
            if ref is None:
                continue
            um = mat_mul(cm, mat_mul(parse_transform(ch.get("transform")),
                                     (1, 0, 0, 1, length(ch.get("x")), length(ch.get("y")))))
            ch, t = ref, ref.tag.replace(NS, "")
        else:
            um = cm
        cst = own_style(ch, t, rules)
        sg = shape_segs(ch, t, cst)
        if not sg:
            if t not in ("title", "desc"):
                doc.dropped.append((tag, f"clip-path #{cid}: <{t}> in it not supported"))
            continue
        segs += transform_segs(sg, mat_mul(um, parse_transform(cst.get("transform"))))
        if rule is None:
            rule = cst.get("clip-rule") or el.get("clip-rule") or "nonzero"
    if not segs:
        return {"segs": [("M", 0.0, 0.0), ("Z",)], "rule": "nonzero"}      # clips everything away
    return {"segs": segs, "rule": rule or "nonzero"}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def filter_of(doc, fid, m, defs, rules, tag):
    """filter=url(#fid) -> (shadows [(dx, dy, sigma, rgba)], blur sigma) in the picture's units, or None (reported).
    The filter graph is evaluated symbolically: what it may build is the element's own drawing (SourceGraphic, perhaps
    blurred) over shadows - coloured, offset, blurred copies of its alpha (SourceAlpha): feDropShadow, feGaussianBlur,
    feOffset, feFlood with feComposite "in", feColorMatrix that colours an alpha, feMerge, feBlend "normal" - and the
    common exports (Figma's drop shadow).  Anything else (inner shadows, lighting, turbulence...) is reported."""
    el = defs.get(fid) if fid else None
    if el is None or el.tag.replace(NS, "") != "filter":
        doc.dropped.append((tag, f"filter #{fid} not found"))
        return None
    if (el.get("primitiveUnits") or "userSpaceOnUse") != "userSpaceOnUse":
        doc.dropped.append((tag, f"filter #{fid}: primitiveUnits objectBoundingBox not supported - drawn without it"))
        return None
    k = math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))

    def vec(dx, dy):
        return (m[0] * dx + m[2] * dy, m[1] * dx + m[3] * dy)

    SRC, ALPHA = ("src", 0.0), ("alpha", 0.0, 0.0, 0.0, (0, 0, 0, 255))
    res, last = {}, SRC

    def get(name):
        if name in (None, ""):
            return last
        if name == "SourceGraphic":
            return SRC
        if name == "SourceAlpha":
            return ALPHA
        if name in ("BackgroundImage", "BackgroundAlpha"):
            return ("empty",)
        return res.get(name, ("bad", f"result {name} unknown"))

    def merge(lower, upper):
        out = []
        for t in (lower, upper):
            if t[0] == "merge":
                out += t[1]
            elif t[0] != "empty":
                out.append(t)
        return ("merge", out) if len(out) != 1 else out[0]

    for pr in el:
        t = pr.tag.replace(NS, "") if isinstance(pr.tag, str) else ""
        if not t:
            continue
        pst = own_style(pr, t, rules)
        a = get(pr.get("in"))
        if t == "feGaussianBlur":
            sd = [_num(x) for x in re.split(r"[\s,]+", (pr.get("stdDeviation") or "0").strip()) if x]
            sdv = sum(sd) / len(sd) if sd else 0.0
            if len(sd) == 2 and abs(sd[0] - sd[1]) > 1e-6:
                doc.dropped.append((tag, f"filter #{fid}: an uneven blur {sd} drawn with sigma {sdv:g}"))
            sg = sdv * k
            if a[0] == "src":
                out = ("src", math.hypot(a[1], sg))
            elif a[0] == "alpha":
                out = ("alpha", a[1], a[2], math.hypot(a[3], sg), a[4])
            else:
                out = ("bad", "a blur of a mixture")
        elif t == "feOffset":
            dx, dy = vec(_num(pr.get("dx")), _num(pr.get("dy")))
            out = ("alpha", a[1] + dx, a[2] + dy, a[3], a[4]) if a[0] == "alpha" else ("bad", "an offset of the drawing")
        elif t == "feFlood":
            c = parse_color(pst.get("flood-color", "black")) or (0, 0, 0, 0)
            al = round(c[3] * _num(pst.get("flood-opacity", "1"), 1.0))
            out = ("flood", c[:3] + (al,)) if al > 0 else ("empty",)
        elif t == "feComposite":
            b2 = get(pr.get("in2"))
            op = pr.get("operator") or "over"
            if op == "in" and a[0] == "flood" and b2[0] == "alpha":
                c = a[1]
                out = ("alpha", b2[1], b2[2], b2[3], c[:3] + (round(c[3] * b2[4][3] / 255),))
            elif op == "in" and a[0] == "empty":
                out = ("empty",)
            elif op == "over":
                out = merge(b2, a)
            else:
                out = ("bad", f"feComposite {op}")
        elif t == "feColorMatrix":
            ty = pr.get("type") or "matrix"
            v = [_num(x) for x in re.split(r"[\s,]+", (pr.get("values") or "").strip()) if x]
            if ty == "matrix" and len(v) == 20 and a[0] == "alpha" and all(abs(v[i]) < 1e-9 for i in
                                                                             (0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17)):
                al = max(0.0, min(1.0, v[18] * a[4][3] / 255 + v[19]))   # alpha row: A * alpha (+ an offset)
                if v[18] > 1.5:                       # Figma's "hardAlpha": the alpha made opaque - an alpha still
                    out = a
                else:
                    out = ("alpha", a[1], a[2], a[3], tuple(max(0, min(255, round(x * 255))) for x in (v[4], v[9], v[14]))
                           + (round(al * 255),))
            elif a[0] == "empty":
                out = a
            else:
                out = ("bad", f"feColorMatrix {ty}")
        elif t == "feDropShadow":
            dx, dy = vec(_num(pr.get("dx"), 2.0), _num(pr.get("dy"), 2.0))
            c = parse_color(pst.get("flood-color", "black")) or (0, 0, 0, 0)
            sh = ("alpha", dx, dy, _num(pr.get("stdDeviation"), 2.0) * k, c[:3] + (round(c[3] * _num(pst.get("flood-opacity", "1"), 1.0)),))
            out = merge(sh, a) if a[0] in ("src", "merge") else ("bad", "a drop shadow of a mixture")
        elif t == "feMerge":
            out = ("empty",)
            for nd in pr:
                if isinstance(nd.tag, str) and nd.tag.replace(NS, "") == "feMergeNode":
                    out = merge(out, get(nd.get("in")))
        elif t == "feBlend":
            mode = pr.get("mode") or "normal"
            out = merge(get(pr.get("in2")), a) if mode == "normal" else ("bad", f"feBlend {mode}")
        else:
            out = ("bad", t)
        if out[0] == "bad":
            doc.dropped.append((tag, f"filter #{fid}: {out[1]} not supported - drawn without the filter"))
            return None
        last = out
        if pr.get("result"):
            res[pr.get("result")] = out
    terms = last[1] if last[0] == "merge" else [last]
    srcs = [i for i, t in enumerate(terms) if t[0] == "src"]
    if len(srcs) != 1 or srcs[0] != len(terms) - 1 or any(t[0] not in ("alpha", "src") for t in terms):
        doc.dropped.append((tag, f"filter #{fid}: its result is not the drawing over its shadows - drawn without it"))
        return None
    shadows = [(t[1], t[2], t[3], t[4]) for t in terms[:-1] if t[4][3] > 0]
    if len(shadows) > 4:
        doc.dropped.append((tag, f"filter #{fid}: {len(shadows)} shadows, the first 4 drawn"))
        shadows = shadows[:4]
    blur = terms[-1][1]
    if not shadows and blur <= 0:
        return None
    return (shadows, blur)

# ---- markers: copies of the marker's content at the path's vertices ----------------------------------------------------

def _unit(d):
    if d is None:
        return None
    n = math.hypot(d[0], d[1])
    return (d[0] / n, d[1] / n) if n > 1e-12 else None


def vertices(segs):
    """the path's vertices in order (subpath starts, segment ends): [x, y, incoming direction, outgoing direction]"""
    out = []
    cx = cy = sx = sy = 0.0
    for s in segs:
        op = s[0]
        if op == "M":
            cx, cy = sx, sy = s[1], s[2]
            out.append([cx, cy, None, None])
            continue
        if not out:
            out.append([cx, cy, None, None])
        if op == "Z":
            if abs(cx - sx) > 1e-9 or abs(cy - sy) > 1e-9:
                d = (sx - cx, sy - cy)
                out[-1][3] = d
                out.append([sx, sy, d, None])
            cx, cy = sx, sy
            continue
        if op == "L":
            dout = din = (s[1] - cx, s[2] - cy)
            ex, ey = s[1], s[2]
        elif op == "Q":
            dout, din = (s[1] - cx, s[2] - cy), (s[3] - s[1], s[4] - s[2])
            ex, ey = s[3], s[4]
        elif op == "C":
            dout = (s[1] - cx, s[2] - cy) if (s[1], s[2]) != (cx, cy) else (s[3] - cx, s[4] - cy)
            din = (s[5] - s[3], s[6] - s[4]) if (s[3], s[4]) != (s[5], s[6]) else (s[5] - s[1], s[6] - s[2])
            ex, ey = s[5], s[6]
        else:                                   # "A"
            cs = arc_to_cubics(cx, cy, *s[1:])
            a, z = cs[0], cs[-1]
            dout = (a[1] - cx, a[2] - cy)
            din = (z[-2] - z[-4], z[-1] - z[-3]) if z[0] == "C" else (z[1] - cx, z[2] - cy)
            ex, ey = s[6], s[7]
        out[-1][3] = dout
        out.append([ex, ey, din, None])
        cx, cy = ex, ey
    return out


def _url(v):
    v = (v or "").strip()
    return v[5:-1].strip().lstrip("#") if v.startswith("url(") and v.endswith(")") else None


def markers(doc, segs, cur, m, opa, defs, rules):
    ids = {k: _url(cur.get(k) or cur.get("marker")) for k in ("marker-start", "marker-mid", "marker-end")}
    if not any(ids.values()):
        return
    vs = vertices(segs)
    sw = length(cur.get("stroke-width"), 1.0, 1.0)
    placed = 0
    for i, (x, y, din, dout) in enumerate(vs):
        key = "marker-start" if i == 0 else "marker-end" if i == len(vs) - 1 else "marker-mid"
        mid = ids[key]
        if not mid:
            continue
        mk = defs.get(mid)
        if mk is None or mk.tag.replace(NS, "") != "marker":
            doc.dropped.append(("marker", f"#{mid} not found"))
            continue
        orient = (mk.get("orient") or "0").strip()
        if orient in ("auto", "auto-start-reverse"):
            a, b = _unit(din), _unit(dout)
            d = (a[0] + b[0], a[1] + b[1]) if a and b else (a or b or (1.0, 0.0))
            if math.hypot(*d) < 1e-9:
                d = b or a
            ang = math.degrees(math.atan2(d[1], d[0]))
            if orient == "auto-start-reverse" and i == 0:
                ang += 180.0
        else:
            ang = float(orient.replace("deg", "") or 0)
        vb = nums(mk.get("viewBox"))
        mw, mh = length(mk.get("markerWidth"), 1.0, 3.0), length(mk.get("markerHeight"), 1.0, 3.0)
        k = sw if (mk.get("markerUnits") or "strokeWidth") == "strokeWidth" else 1.0
        if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
            k *= min(mw / vb[2], mh / vb[3])
        r = math.radians(ang)
        M = mat_mul(m, (1.0, 0.0, 0.0, 1.0, x, y))
        M = mat_mul(M, (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0.0, 0.0))
        M = mat_mul(M, (k, 0.0, 0.0, k, 0.0, 0.0))
        M = mat_mul(M, (1.0, 0.0, 0.0, 1.0, -length(mk.get("refX")), -length(mk.get("refY"))))
        inh = {"fill": "black", "stroke": "none", "stroke-width": "1"}
        mst = own_style(mk, "marker", rules)
        for kk in INHERITED:
            if kk in mst and mst[kk] != "inherit":
                inh[kk] = mst[kk]
        for ch in mk:
            t = ch.tag.replace(NS, "") if isinstance(ch.tag, str) else ""
            if t and t not in SKIP_SUBTREE and t not in UNSUPPORTED:
                walk(doc, ch, t, inh, M, opa, defs, rules, True)
        placed += 1
    if placed:
        doc.converted.append(("marker", f"{placed} placed as paths"))


# ---- text: glyph outlines of a TrueType font -----------------------------------------------------------------------

_FONTS = {}


def _font(cur, doc=None):
    import ttf
    fams = [f for f in (cur.get("font-family") or "sans-serif").split(",") if f.strip()]
    for f in fams:                               # the file's own @font-face first
        fp = doc.fontfaces.get(f.strip().strip("'\"").lower()) if doc else None
        if fp:
            if fp not in _FONTS:
                try:
                    _FONTS[fp] = ttf.Font(fp)
                except Exception as e:           # noqa: BLE001 - a bad font file is reported, the next family tried
                    doc.dropped.append(("text", f"@font-face {fp}: {e}"))
                    _FONTS[fp] = None
            if _FONTS[fp]:
                return _FONTS[fp]
    w = (cur.get("font-weight") or "normal").strip()
    bold = w in ("bold", "bolder") or (w.isdigit() and int(w) >= 600)
    italic = (cur.get("font-style") or "normal").strip() in ("italic", "oblique")
    key = (tuple(f.strip().lower() for f in fams), bold, italic)
    if key not in _FONTS:
        f = ttf.find(fams, bold, italic)
        if f is None and not any(x.strip().strip("'\"").lower() == "sans-serif" for x in fams):
            f = ttf.find(["sans-serif"], bold, italic)
        _FONTS[key] = f
    return _FONTS[key]


def text_shapes(doc, el, cur, m, opa, defs, rules):
    """<text> and its <tspan>s -> paths: runs of characters, each in its own style, laid out one glyph after another on
    the baseline (no kerning, no shaping); text-anchor moves the whole line"""
    import ttf
    runs = []                                    # [text, style, x or None, y or None]

    def first(v):
        n = nums(v)
        return n[0] if n else None

    def collect(e, c, x, y):
        if e.text:
            runs.append([e.text, c, x, y])
            x = y = None
        for ch in e:
            t = ch.tag.replace(NS, "") if isinstance(ch.tag, str) else ""
            if t == "tspan":
                st = own_style(ch, t, rules)
                c2 = dict(c)
                for kk in INHERITED:
                    if kk in st and st[kk] != "inherit":
                        c2[kk] = st[kk]
                collect(ch, c2, first(ch.get("x")), first(ch.get("y")))
            elif t:
                doc.dropped.append((t, "inside <text>: not drawn"))
            if ch.tail:
                runs.append([ch.tail, c, None, None])

    collect(el, cur, first(el.get("x")) or 0.0, first(el.get("y")) or 0.0)
    if not runs:
        return
    keep = el.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
    for r in runs:                               # xml:space default: whitespace collapsed
        r[0] = r[0].replace("\n", " ").replace("\t", " ")
        if not keep:
            r[0] = re.sub(" +", " ", r[0])
    if not keep:
        runs[0][0] = runs[0][0].lstrip()
        runs[-1][0] = runs[-1][0].rstrip()
    anchor = (cur.get("text-anchor") or "start").strip()
    x, y = runs[0][2] or 0.0, runs[0][3] or 0.0
    lines, line = [], []
    for r in runs:                               # a run with its own x or y starts a new chunk (anchored on its own)
        if line and (r[2] is not None or r[3] is not None):
            lines.append(line)
            line = []
        line.append(r)
    lines.append(line)
    for line in lines:
        x = line[0][2] if line[0][2] is not None else x
        y = line[0][3] if line[0][3] is not None else y
        laid, total = [], 0.0
        for s, c, _x, _y in line:
            if not s:
                continue
            f = _font(c, doc)
            if f is None:
                doc.dropped.append(("text", f"no TrueType font for '{c.get('font-family') or 'sans-serif'}'"))
                continue
            size = length(c.get("font-size"), 16.0, 16.0)
            sp = length(c.get("letter-spacing"), size, 0.0) if (c.get("letter-spacing") or "normal") != "normal" else 0.0
            _segs, adv, missing = ttf.text_segs(f, s, 0.0, 0.0, size, "start", sp)
            if missing:
                doc.dropped.append(("text", f"no glyph for {''.join(missing)!r} in {f.family} {f.style}"))
            laid.append((s, c, f, size, sp))
            total += adv
        x0 = x - (total / 2 if anchor == "middle" else total if anchor == "end" else 0.0)
        for s, c, f, size, sp in laid:
            segs, adv, _ = ttf.text_segs(f, s, x0, y, size, "start", sp)
            if segs:
                c2 = dict(c)
                c2["fill-rule"] = "nonzero"              # TrueType outlines wind non-zero
                emit(doc, "text", segs, c2, m, opa, defs, rules)
                doc.converted.append(("text", f"{s!r} -> outlines, {f.family} {f.style} {size:g} px"))
            x0 += adv
        x = x0


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    d = load(sys.argv[1])
    print(f"size {d.w:g} x {d.h:g}, viewBox {' '.join(f'{v:g}' for v in d.vb)}, {len(d.shapes)} shapes")
    for i, s in enumerate(d.shapes):
        ops = "".join(seg[0] for seg in s["segs"])
        print(f"  {i:3d} {s['tag']:8s} fill {s['fill'] and s['fill'][0]} stroke {s['stroke'] and s['stroke'][0]}"
              f" w {s['width']:.2f} {s['rule']} segs {len(s['segs'])} {ops[:60]}")
    for what, how in d.converted:
        print(f"  converted {what}: {how}")
    for what, why in d.dropped:
        print(f"  dropped {what}: {why}")


if __name__ == "__main__":
    main()
