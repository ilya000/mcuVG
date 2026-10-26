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

What it cannot express is reported, never silently drawn wrong: mask, clipPath, filter, image, text, pattern, marker,
dashes, gradient spread other than pad.  The encoder decides what to do with each report (refuse, or drop and warn).
"""
import math
import re
import sys
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"

NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "lime": (0, 255, 0), "green": (0, 128, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "cyan": (0, 255, 255), "aqua": (0, 255, 255),
    "magenta": (255, 0, 255), "fuchsia": (255, 0, 255), "gray": (128, 128, 128), "grey": (128, 128, 128),
    "silver": (192, 192, 192), "maroon": (128, 0, 0), "olive": (128, 128, 0), "purple": (128, 0, 128),
    "teal": (0, 128, 128), "navy": (0, 0, 128), "orange": (255, 165, 0), "pink": (255, 192, 203),
    "brown": (165, 42, 42), "gold": (255, 215, 0), "transparent": None,
}

INHERITED = ("fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "fill-rule",
             "fill-opacity", "stroke-opacity", "color", "stroke-dasharray", "visibility", "display")
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
    rgb = NAMED.get(s.lower(), (0, 0, 0))
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
        self.shapes = []           # dicts: segs, fill, stroke, width, cap, join, miter, rule
        self.dropped = []          # (what, why)


def css_rules(root):
    rules = {}
    for st in root.iter(NS + "style"):
        txt = re.sub(r"/\*.*?\*/", "", st.text or "", flags=re.S)
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", txt):
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
    if (attr("spreadMethod") or "pad") != "pad":
        doc.dropped.append(("paint", f"#{gid}: spreadMethod {attr('spreadMethod')} drawn as pad"))
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
        return ("linear", mat_apply(full, *p1), mat_apply(full, *p2), stops)
    ref = math.hypot(ref_w, ref_h) / math.sqrt(2)
    c = (length(attr("cx"), ref_w, 0.5 * ref_w), length(attr("cy"), ref_h, 0.5 * ref_h))
    r = length(attr("r"), ref, 0.5 * ref)
    f = (length(attr("fx"), ref_w, c[0]), length(attr("fy"), ref_h, c[1]))
    C = mat_apply(full, *c)
    R = (mat_apply(full, c[0] + r, c[1]), mat_apply(full, c[0], c[1] + r))
    rx, ry = math.dist(C, R[0]), math.dist(C, R[1])
    if abs(rx - ry) > 0.02 * max(rx, ry):
        doc.dropped.append(("paint", f"#{gid}: elliptic radial gradient drawn as circular"))
    return ("radial", C, (rx + ry) / 2, stops, mat_apply(full, *f))


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
UNSUPPORTED = ("image", "text", "foreignObject", "switch")


def walk(doc, el, tag, inh, m, opa, defs, rules, in_use):
    st = own_style(el, tag, rules)
    cur = dict(inh)
    for k in INHERITED:
        if k in st and st[k] != "inherit":
            cur[k] = st[k]
    if cur.get("display") == "none" or st.get("display") == "none":
        return
    m = mat_mul(m, parse_transform(st.get("transform")))
    opa = opa * float(st.get("opacity", "1"))
    for k in ("clip-path", "mask", "filter"):
        if st.get(k) and st[k] != "none":
            doc.dropped.append((tag, f"{k}={st[k]} ignored"))
    if tag in ("svg", "g", "a"):
        if tag == "svg" and el is not None and doc.shapes is not None and el.get("x"):
            m = mat_mul(m, (1, 0, 0, 1, length(el.get("x")), length(el.get("y"))))
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
    segs = shape_segs(el, tag, st)
    if not segs:
        if tag not in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"):
            doc.dropped.append((tag, "unknown element"))
        return
    if cur.get("visibility") in ("hidden", "collapse"):
        return
    bbox = segs_bbox(segs)
    color = cur.get("color")

    def paint(key, okey):
        c = parse_color(cur.get(key), color)
        if c is None:
            return None
        k = opa * float(cur.get(okey, "1"))
        if c[0] == "url":
            p = gradient(doc, defs, c[1], bbox, m, rules)
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
        if cur.get("stroke-dasharray") not in (None, "none"):
            doc.dropped.append((tag, "dashes drawn as a solid line"))
    if fill is None and (stroke is None or width <= 0):
        return
    doc.shapes.append({
        "segs": transform_segs(segs, m),
        "fill": fill,
        "stroke": stroke if width > 0 else None,
        "width": width,
        "cap": cur.get("stroke-linecap", "butt"),
        "join": cur.get("stroke-linejoin", "miter"),
        "miter": float(cur.get("stroke-miterlimit", "4")),
        "rule": cur.get("fill-rule", "nonzero"),
        "tag": tag,
    })


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
    for what, why in d.dropped:
        print(f"  dropped {what}: {why}")


if __name__ == "__main__":
    main()
