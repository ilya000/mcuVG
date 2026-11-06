#!/usr/bin/env python3
"""mcuVG - the C rasteriser (src/mcuvg_raster.c) on the host, through ctypes: the same C, built
for the host, driven from Python.  Used by the checks to draw shapes from svgfront and compare them with resvg.

    lib = vg_host.Lib()                 # builds build/vector_host/libmcuvg.dylib when the C is newer
    img = lib.draw(doc, size)           # svgfront.Doc -> RGBA bytes (premultiplied), size x size
"""
import ctypes as C
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "build", "host")

FLAT, LINEAR, RADIAL = 0, 1, 2
RGBA8888, RGB565, RGB565_SWAP, A8 = 1, 2, 3, 4
NONZERO, EVENODD = 0, 1
CAPS = {"butt": 0, "round": 1, "square": 2}
JOINS = {"miter": 0, "miter-clip": 0, "arcs": 0, "round": 1, "bevel": 2}


class Paint(C.Structure):
    _fields_ = [("kind", C.c_uint8), ("n", C.c_uint8), ("color", C.c_uint32),
                ("x0", C.c_float), ("y0", C.c_float), ("x1", C.c_float), ("y1", C.c_float),
                ("off", C.c_float * 8), ("col", C.c_uint32 * 8),
                ("spread", C.c_uint8), ("focal", C.c_uint8), ("ry", C.c_float), ("rot", C.c_float),
                ("fx", C.c_float), ("fy", C.c_float), ("fr", C.c_float)]


class Shadow(C.Structure):
    _fields_ = [("dx", C.c_float), ("dy", C.c_float), ("sigma", C.c_float), ("color", C.c_uint32)]


class Group(C.Structure):
    _fields_ = [("x0", C.c_float), ("y0", C.c_float), ("x1", C.c_float), ("y1", C.c_float),
                ("opacity", C.c_float), ("blur", C.c_float), ("nshadow", C.c_uint32), ("shadow", Shadow * 4)]


SPREAD = {"pad": 0, "reflect": 1, "repeat": 2}


class Mat(C.Structure):
    _fields_ = [(k, C.c_float) for k in "abcdef"]


def argb(c):
    r, g, b, a = c
    return (a << 24) | (r << 16) | (g << 8) | b


def paint_of(p):
    """svgfront paint -> Paint"""
    q = Paint()
    if p[0] == "flat":
        q.kind, q.color = FLAT, argb(p[1])
        return q
    stops = p[3][:8]
    if len(stops) == 1:
        q.kind, q.color = FLAT, argb(stops[0][1])
        return q
    q.n = len(stops)
    for i, (o, c) in enumerate(stops):
        q.off[i], q.col[i] = o, argb(c)
    import svgfront
    spread, focal, fr, ry, rot = svgfront.paint_ext(p)
    q.spread = SPREAD[spread]
    if p[0] == "linear":
        q.kind = LINEAR
        (q.x0, q.y0), (q.x1, q.y1) = p[1], p[2]
    else:
        q.kind = RADIAL
        (q.x0, q.y0), q.x1 = p[1], p[2]
        if focal is not None:
            q.focal, (q.fx, q.fy), q.fr = 1, focal, fr
        if ry is not None:
            q.ry, q.rot = ry, rot
    return q


def group_of(g):
    """svgfront / decoded group -> Group (its box: the file's, or computed as the encoder does)"""
    import svgfront
    b = g.get("bbox") or svgfront.group_box(g) or (0.0, 0.0, 0.0, 0.0)
    q = Group()
    q.x0, q.y0, q.x1, q.y1 = b
    q.opacity, q.blur = g["opacity"], g["blur"]
    q.nshadow = len(g["shadows"][:4])
    for i, (dx, dy, sg, c) in enumerate(g["shadows"][:4]):
        q.shadow[i].dx, q.shadow[i].dy, q.shadow[i].sigma, q.shadow[i].color = dx, dy, sg, argb(c)
    return q


def _build(so, srcs, cc, extra):
    """the C library for this host: cc / gcc / clang (macOS, Linux, MinGW on Windows) or MSVC's cl"""
    import shutil
    cands = [cc] if cc else []
    cands += [os.environ.get("CC", "")] + (["cl", "gcc", "clang", "cc"] if os.name == "nt" else ["cc", "clang", "gcc"])
    for c in cands:
        if not c or not shutil.which(c):
            continue
        if os.path.basename(c).lower().startswith("cl"):
            cmd = [c, "/nologo", "/O2", "/LD"] + list(extra) + srcs + ["/Fe:" + so, "/link", "/DEF:" + _msvc_def(so)]
            subprocess.run(cmd, check=True, cwd=os.path.dirname(so))
        else:
            subprocess.run([c, "-O2", "-std=c99", "-shared", "-fPIC"] + list(extra) + ["-o", so] + srcs, check=True)
        return
    raise RuntimeError("no C compiler found (cc, gcc, clang or cl): the converter still works, without memory figures")


def _msvc_def(so):
    """MSVC exports nothing by default: a .def file with the public functions"""
    names = []
    for h in ("mcuvg_raster.h", "mcuvg.h"):
        for line in open(os.path.join(SRC, h)):
            line = line.strip()
            if line.startswith(("int mcuvg_", "void mcuvg_", "size_t mcuvg_", "float mcuvg_")) and "(" in line:
                names.append(line.split("(")[0].split()[-1].lstrip("*"))
    d = os.path.splitext(so)[0] + ".def"
    with open(d, "w") as f:
        f.write("EXPORTS\n" + "".join(f"    {n}\n" for n in names))
    return d


class Lib:
    def __init__(self, cc=None, extra=()):
        os.makedirs(OUT, exist_ok=True)
        ext = ".dll" if os.name == "nt" else ".dylib" if sys.platform == "darwin" else ".so"
        so = os.path.join(OUT, "libmcuvg%s%s" % ("_" + "_".join(e.strip("-").replace("=", "") for e in extra) if extra else "", ext))
        srcs = [os.path.join(SRC, f) for f in ("mcuvg_raster.c", "mcuvg.c")]
        hdrs = [os.path.join(SRC, f) for f in ("mcuvg_raster.h", "mcuvg.h")]
        if not os.path.exists(so) or any(os.path.getmtime(s) > os.path.getmtime(so) for s in srcs + hdrs):
            _build(so, srcs, cc, extra)
        self.L = C.CDLL(so)
        L = self.L
        L.mcuvg_mem.restype = C.c_size_t
        L.mcuvg_mem.argtypes = [C.c_int, C.c_uint32, C.c_uint32]
        L.mcuvg_init.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_size_t]
        for f in ("mcuvg_move", "mcuvg_line"):
            getattr(L, f).argtypes = [C.c_void_p, C.c_float, C.c_float]
        L.mcuvg_quad.argtypes = [C.c_void_p] + [C.c_float] * 4
        L.mcuvg_cubic.argtypes = [C.c_void_p] + [C.c_float] * 6
        L.mcuvg_arc.argtypes = [C.c_void_p, C.c_float, C.c_float, C.c_float, C.c_int, C.c_int, C.c_float, C.c_float]
        L.mcuvg_close.argtypes = L.mcuvg_begin.argtypes = [C.c_void_p]
        L.mcuvg_set.argtypes = [C.c_void_p, C.POINTER(Mat)]
        L.mcuvg_fill.argtypes = [C.c_void_p, C.POINTER(Paint), C.c_int]
        L.mcuvg_stroke.argtypes = [C.c_void_p, C.POINTER(Paint), C.c_float, C.c_int, C.c_int, C.c_float]
        L.mcuvg_tolerance.argtypes = [C.c_void_p, C.c_float]
        L.mcuvg_usage.argtypes = [C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32)]
        L.mcuvg_draw.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32, C.c_void_p, C.c_uint32]
        L.mcuvg_fit.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32] + [C.c_float] * 5
        self.v2 = hasattr(L, "mcuvg_group_begin")       # built with version 2 (not with -DMCUVG_V2=0)
        if self.v2:
            L.mcuvg_dash.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_int, C.c_float]
            L.mcuvg_layers.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
            for f in ("mcuvg_shadow_begin", "mcuvg_group_begin"):
                getattr(L, f).argtypes = [C.c_void_p, C.POINTER(Group)]
            for f in ("mcuvg_shadow_end", "mcuvg_group_end"):
                getattr(L, f).argtypes = [C.c_void_p]
            L.mcuvg_clip_path.argtypes = [C.c_void_p, C.c_int]
            L.mcuvg_layers_used.argtypes = [C.c_void_p]
            L.mcuvg_layers_used.restype = C.c_size_t
        self.ctx = C.create_string_buffer(16384)          # sizeof(mcuvg_ctx) with version 2's frames
        self.layers = C.create_string_buffer(8 << 20)
        self.layers_limit = None                          # bytes of layer memory to give (a small device), None: all

    def context(self, px, w, h, stride, fmt, points=8192):
        n = self.L.mcuvg_mem(w, points, 3 * points)
        self.mem = C.create_string_buffer(n)
        rc = self.L.mcuvg_init(self.ctx, px, w, h, stride, fmt, self.mem, n)
        assert rc == 0, rc
        if self.v2:
            n = len(self.layers) if self.layers_limit is None else min(self.layers_limit, len(self.layers))
            self.L.mcuvg_layers(self.ctx, self.layers if n else None, n)
        return self.ctx

    def path(self, segs):
        L, c = self.L, self.ctx
        L.mcuvg_begin(c)
        for s in segs:
            op = s[0]
            if op == "M":
                L.mcuvg_move(c, s[1], s[2])
            elif op == "L":
                L.mcuvg_line(c, s[1], s[2])
            elif op == "Q":
                L.mcuvg_quad(c, *s[1:])
            elif op == "C":
                L.mcuvg_cubic(c, *s[1:])
            elif op == "A":
                L.mcuvg_arc(c, s[1], s[2], s[3], int(s[4]), int(s[5]), s[6], s[7])
            elif op == "Z":
                L.mcuvg_close(c)

    def target(self, w, h, fmt):
        bpp = 4 if fmt == RGBA8888 else 2 if fmt in (RGB565, RGB565_SWAP) else 1
        buf = C.create_string_buffer(w * h * bpp)
        self.context(buf, w, h, w * bpp, fmt)
        return buf

    def usage(self):
        """(points, edges): the busiest path since the context was made"""
        p, e = C.c_uint32(), C.c_uint32()
        self.L.mcuvg_usage(self.ctx, C.byref(p), C.byref(e))
        return p.value, e.value

    def layers_used(self):
        """the most layer memory (version 2) held at once since the context was made"""
        return self.L.mcuvg_layers_used(self.ctx) if self.v2 else 0

    def block_size(self, w, points, edges):
        return self.L.mcuvg_mem(w, points, edges)

    def draw_mcuvg(self, data, w, h, scale, fmt=RGBA8888, recolor=None, deg=0.0):
        """the C decoder: the picture's design units times scale (turned by deg about the target's centre), into a
        w x h target; recolor: colours 0xAARRGGBB replacing the picture's table -> (bytes, rc)"""
        buf = self.target(w, h, fmt)
        m = Mat(scale, 0, 0, scale, 0, 0)
        self.L.mcuvg_set(self.ctx, C.byref(m))
        if deg:
            import math
            r = math.radians(deg)
            ca, sa = math.cos(r), math.sin(r)
            cx, cy = w / 2.0, h / 2.0                       # turn about the centre: T(c) R S T(-c / s) in design units
            m = Mat(scale * ca, scale * sa, -scale * sa, scale * ca,
                    cx - ca * cx + sa * cy, cy - sa * cx - ca * cy)
            self.L.mcuvg_set(self.ctx, C.byref(m))
        rec = None
        if recolor:
            rec = (C.c_uint32 * len(recolor))(*recolor)
        rc = self.L.mcuvg_draw(self.ctx, data, len(data), rec, len(recolor) if recolor else 0)
        return buf.raw, rc

    def draw(self, doc, size, fmt=RGBA8888, bg=None):
        """the whole document scaled so its viewBox fills size x size (the longer side), as resvg's fitTo width"""
        w = h = size
        k = size / doc.vb[2]
        h = round(doc.vb[3] * k)
        bpp = 4 if fmt == RGBA8888 else 2 if fmt in (RGB565, RGB565_SWAP) else 1
        buf = C.create_string_buffer((bg or b"\0" * bpp) * (w * h) if bg else w * h * bpp)
        self.context(buf, w, h, w * bpp, fmt)
        m = Mat(k, 0, 0, k, 0, 0)
        self.L.mcuvg_set(self.ctx, C.byref(m))
        self.items(doc.shapes)
        return buf.raw, w, h

    def items(self, items):
        """a tree of shapes and version 2 groups, as the C decoder draws it: a group with shadows twice"""
        L, c = self.L, self.ctx
        for s in items:
            if "group" in s:
                g = group_of(s)
                if s["shadows"]:
                    assert L.mcuvg_shadow_begin(c, C.byref(g)) == 0, "shadow_begin"
                    self.clip(s)
                    self.items(s["items"])
                    assert L.mcuvg_shadow_end(c) == 0, "shadow_end"
                assert L.mcuvg_group_begin(c, C.byref(g)) == 0, "group_begin"
                self.clip(s)
                self.items(s["items"])
                assert L.mcuvg_group_end(c) == 0, "group_end"
                continue
            self.path(s["segs"])
            if s["fill"] is not None:
                rc = L.mcuvg_fill(c, C.byref(paint_of(s["fill"])), EVENODD if s["rule"] == "evenodd" else NONZERO)
                assert rc == 0, f"fill: {rc}"
            if s["stroke"] is not None:
                if s.get("dash"):
                    v, off = s["dash"]
                    L.mcuvg_dash(c, (C.c_float * len(v))(*v), len(v), off)
                rc = L.mcuvg_stroke(c, C.byref(paint_of(s["stroke"])), s["width"], CAPS.get(s["cap"], 0),
                                    JOINS.get(s["join"], 0), s["miter"])
                L.mcuvg_dash(c, None, 0, 0.0)
                assert rc == 0, f"stroke: {rc}"

    def clip(self, g):
        if g["clip"] is not None:
            self.path(g["clip"]["segs"])
            assert self.L.mcuvg_clip_path(self.ctx, EVENODD if g["clip"]["rule"] == "evenodd" else NONZERO) == 0, "clip"
