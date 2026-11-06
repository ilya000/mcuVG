/*
 * mcuVG - the path rasteriser (mcuvg_raster.h): flattening, strokes, exact area coverage, paints.
 * Copyright (c) 2026 Ilya Osipov. MIT licence (see LICENSE).
 *
 * Coverage.  Each edge adds its signed area to the cells of a row (the accumulation method of font-rs / stb_truetype
 * v2): a running sum along the row gives every pixel the area of the shape inside it, exactly, for any slope.  The
 * non-zero rule is min(|sum|, 1), even-odd folds the sum.  Shapes that overlap THEMSELVES inside one pixel (a stroke's
 * inner joins) are counted twice there and clipped at 1 - inside the stroke, where it does not show.
 *
 * Rows, not a picture.  Edges are sorted by their top; a row takes the edges that reach it into the active list,
 * adds them, sums, paints the covered spans and clears what it touched.  Memory: the row (w + 2 floats), the edges
 * (16 B), the active list (2 B an edge), the path's points (8 B) - all inside the block the caller gave.
 */
#include "mcuvg_raster.h"

#define MARK 1e30f                         /* a point with this x ends a subpath; its y: 1 closed, 0 open */
#define PI_F 3.14159265f

/* ---- maths ---------------------------------------------------------------------------------------------------------- */

static inline float fabs_(float v) { return v < 0 ? -v : v; }
static inline float fmin_(float a, float b) { return a < b ? a : b; }
static inline float fmax_(float a, float b) { return a > b ? a : b; }
static inline int ifloor(float v) { int i = (int)v; return (float)i > v ? i - 1 : i; }
static inline int iceil(float v) { int i = (int)v; return (float)i < v ? i + 1 : i; }

float mcuvg_sqrt(float v)
{
    if (!(v > 0.0f)) return 0.0f;
#if defined(__arm__) && defined(__ARM_FP) && (__ARM_FP & 4)
    float r;
    __asm("vsqrt.f32 %0, %1" : "=t"(r) : "t"(v));
    return r;
#else
    return __builtin_sqrtf(v);
#endif
}

/* sin and cos on |x| <= pi/4 (Taylor to x^9 / x^8: below 1e-7 there), the argument brought there in one step */
static float sin_k(float x) { float s = x * x; return x * (1.0f + s * (-1.6666667e-1f + s * (8.3333333e-3f + s * (-1.9841270e-4f + s * 2.7557319e-6f)))); }
static float cos_k(float x) { float s = x * x; return 1.0f + s * (-0.5f + s * (4.1666667e-2f + s * (-1.3888889e-3f + s * 2.4801587e-5f))); }
static void sincos_(float a, float *s, float *c)
{
    float k = a * 0.63661977f;                         /* quarter turns */
    int q = (int)(k < 0 ? k - 0.5f : k + 0.5f);
    float r = (a - (float)q * 1.5707963705062866f) - (float)q * -4.371139000186241e-8f;
    float sr = sin_k(r), cr = cos_k(r);
    switch (q & 3) {
    case 0: *s = sr; *c = cr; break;
    case 1: *s = cr; *c = -sr; break;
    case 2: *s = -sr; *c = -cr; break;
    default: *s = -cr; *c = sr; break;
    }
}
float mcuvg_sin(float a) { float s, c; sincos_(a, &s, &c); return s; }
float mcuvg_cos(float a) { float s, c; sincos_(a, &s, &c); return c; }

/* atan on [0, 1]: Abramowitz & Stegun 4.4.49, error below 2e-8 */
float mcuvg_atan2(float y, float x)
{
    float ax = fabs_(x), ay = fabs_(y);
    if (ax == 0.0f && ay == 0.0f) return 0.0f;
    float a = ax < ay ? ax / ay : ay / ax, s = a * a;
    float r = a * (1.0f + s * (-0.3333314528f + s * (0.1999355085f + s * (-0.1420889944f + s * (0.1065626393f +
              s * (-0.0752896400f + s * (0.0429096138f + s * (-0.0161657367f + s * 0.0028662257f))))))));
    if (ay > ax) r = 1.57079633f - r;
    if (x < 0) r = PI_F - r;
    return y < 0 ? -r : r;
}

/* ---- the context ---------------------------------------------------------------------------------------------------- */

static size_t align8(size_t v) { return (v + 7u) & ~(size_t)7u; }

size_t mcuvg_mem(int w, uint32_t points, uint32_t edges)
{
    if (points < (edges + 2u) / 3u) points = (edges + 2u) / 3u;     /* init gives three edges a point: enough of both */
    edges = points * 3u;
    return align8((size_t)points * 8u) + align8((size_t)edges * sizeof(mcuvg_edge)) + align8((size_t)edges * 2u) +
           align8((size_t)(w + 2) * 4u) + align8((size_t)w * 4u) + 256u * 4u + align8((size_t)((w + 2 + 31) / 32 + 1) * 4u) + 16u;
}

int mcuvg_init(mcuvg_ctx *c, void *px, int w, int h, int stride, int fmt, void *mem, size_t len)
{
    if (!c || w <= 0 || h <= 0 || !mem || fmt < MCUVG_RGBA8888 || fmt > MCUVG_SPAN) return MCUVG_E_ARG;
    uint8_t *p = (uint8_t *)(((uintptr_t)mem + 7u) & ~(uintptr_t)7u);
    size_t fixed = align8((size_t)(w + 2) * 4u) + align8((size_t)w * 4u) + 256u * 4u +
                   align8((size_t)((w + 2 + 31) / 32 + 1) * 4u) + 16u;
    if (len < fixed + 8u * (8u + 3u * (sizeof(mcuvg_edge) + 2u))) return MCUVG_E_ARG;   /* eight points at least */
    /* what is left after the row, the span and the gradient's table is shared: 8 B a point, 3 edges of 18 B a point */
    uint32_t points = (uint32_t)((len - fixed) / (8u + 3u * (sizeof(mcuvg_edge) + 2u)));
    if (points * 3u > 65535u) points = 65535u / 3u;
    c->px = px; c->w = w; c->h = h; c->stride = stride; c->fmt = fmt;
    c->span = 0; c->user = 0;
    c->cx0 = 0; c->cy0 = 0; c->cx1 = w; c->cy1 = h;
    c->tol = 0.1f;
    c->hw_pt = c->hw_e = 0;
    c->row = (float *)p;            p += align8((size_t)(w + 2) * 4u);
    c->pix = (uint32_t *)p;         p += align8((size_t)w * 4u);
    p += 256u * 4u;                 /* the gradient's table sits right after the span: c->pix + w ... see lut() */
    c->tb = (uint32_t *)p;          p += align8((size_t)((w + 2 + 31) / 32 + 1) * 4u);
    c->pt = (float *)p;             p += align8((size_t)points * 8u);
    c->cappt = points;
    c->e = (mcuvg_edge *)p;         p += align8((size_t)points * 3u * sizeof(mcuvg_edge));
    c->cap = points * 3u;
    c->act = (uint16_t *)p;
    for (int i = 0; i < w + 2; i++) c->row[i] = 0.0f;
    for (int i = 0; i < (w + 2 + 31) / 32 + 1; i++) c->tb[i] = 0;
    mcuvg_identity(c);
    mcuvg_begin(c);
#if MCUVG_V2
    c->ndash = 0; c->dash_off = 0;
    c->mask = 0; c->mx0 = c->my0 = c->mw = c->mh = 0; c->ox = c->oy = 0;
    c->lm = 0; c->llen = c->lused = c->lhw = 0; c->depth = 0;
#endif
    return MCUVG_OK;
}

void mcuvg_tolerance(mcuvg_ctx *c, float px) { c->tol = px > 0.01f ? px : 0.01f; }

static uint32_t *lut_of(mcuvg_ctx *c) { return (uint32_t *)((uint8_t *)c->pix + align8((size_t)c->w * 4u)); }

void mcuvg_usage(const mcuvg_ctx *c, uint32_t *points, uint32_t *edges)
{
    if (points) *points = c->hw_pt;
    if (edges) *edges = c->hw_e;
}

void mcuvg_span(mcuvg_ctx *c, mcuvg_span_fn fn, void *user) { c->span = fn; c->user = user; }

void mcuvg_clip(mcuvg_ctx *c, int x0, int y0, int x1, int y1)
{
    c->cx0 = x0 < 0 ? 0 : x0;
    c->cy0 = y0 < 0 ? 0 : y0;
    c->cx1 = x1 > c->w ? c->w : x1;
    c->cy1 = y1 > c->h ? c->h : y1;
}

/* ---- the matrix ----------------------------------------------------------------------------------------------------- */

static mcuvg_mat mul(const mcuvg_mat *a, const mcuvg_mat *b)          /* b first, then a */
{
    mcuvg_mat r = { a->a * b->a + a->c * b->b, a->b * b->a + a->d * b->b,
                    a->a * b->c + a->c * b->d, a->b * b->c + a->d * b->d,
                    a->a * b->e + a->c * b->f + a->e, a->b * b->e + a->d * b->f + a->f };
    return r;
}
void mcuvg_identity(mcuvg_ctx *c) { mcuvg_mat m = { 1, 0, 0, 1, 0, 0 }; c->m = m; }
void mcuvg_set(mcuvg_ctx *c, const mcuvg_mat *m) { c->m = *m; }
void mcuvg_concat(mcuvg_ctx *c, const mcuvg_mat *m) { c->m = mul(&c->m, m); }
void mcuvg_translate(mcuvg_ctx *c, float x, float y) { mcuvg_mat m = { 1, 0, 0, 1, x, y }; mcuvg_concat(c, &m); }
void mcuvg_scale(mcuvg_ctx *c, float sx, float sy) { mcuvg_mat m = { sx, 0, 0, sy, 0, 0 }; mcuvg_concat(c, &m); }
void mcuvg_rotate(mcuvg_ctx *c, float deg)
{
    float s, k;
    sincos_(deg * (PI_F / 180.0f), &s, &k);
    mcuvg_mat m = { k, s, -s, k, 0, 0 };
    mcuvg_concat(c, &m);
}
static inline void xf(const mcuvg_ctx *c, float x, float y, float *X, float *Y)
{
    *X = c->m.a * x + c->m.c * y + c->m.e;
    *Y = c->m.b * x + c->m.d * y + c->m.f;
}
static float mat_scale(const mcuvg_mat *m)                            /* the mean scale: sqrt |det| */
{
    return mcuvg_sqrt(fabs_(m->a * m->d - m->b * m->c));
}

/* ---- the path ------------------------------------------------------------------------------------------------------- */

static void put(mcuvg_ctx *c, float x, float y)
{
    if (c->npt >= c->cappt) { c->full = 1; return; }
    if (c->npt > c->sub) {                                             /* a repeat of the last point adds nothing */
        const float *l = c->pt + 2u * (c->npt - 1u);
        if (l[0] == x && l[1] == y) return;
    }
    c->pt[2u * c->npt] = x;
    c->pt[2u * c->npt + 1u] = y;
    if (++c->npt > c->hw_pt) c->hw_pt = c->npt;
}
static void end_sub(mcuvg_ctx *c, int closed)
{
    if (!c->open) return;
    c->open = 0;
    if (c->npt >= c->cappt) { c->full = 1; return; }
    c->pt[2u * c->npt] = MARK;
    c->pt[2u * c->npt + 1u] = closed ? 1.0f : 0.0f;
    if (++c->npt > c->hw_pt) c->hw_pt = c->npt;
}
void mcuvg_begin(mcuvg_ctx *c) { c->npt = 0; c->sub = 0; c->open = 0; c->full = 0; c->ux = c->uy = c->usx = c->usy = 0; }

void mcuvg_move(mcuvg_ctx *c, float x, float y)
{
    end_sub(c, 0);
    float X, Y;
    xf(c, x, y, &X, &Y);
    c->sub = c->npt;
    c->open = 1;
    put(c, X, Y);
    c->ux = c->usx = x;
    c->uy = c->usy = y;
}
static void ensure_open(mcuvg_ctx *c) { if (!c->open) mcuvg_move(c, c->ux, c->uy); }

void mcuvg_line(mcuvg_ctx *c, float x, float y)
{
    ensure_open(c);
    float X, Y;
    xf(c, x, y, &X, &Y);
    put(c, X, Y);
    c->ux = x;
    c->uy = y;
}

static int segs_for(float dd, float k, float tol)                     /* pieces so that the error k * dd / n^2 <= tol */
{
    float n = mcuvg_sqrt(k * dd / tol);
    int i = iceil(n);
    return i < 1 ? 1 : i > 256 ? 256 : i;
}

void mcuvg_quad(mcuvg_ctx *c, float x1, float y1, float x, float y)
{
    ensure_open(c);
    float X0, Y0, X1, Y1, X2, Y2;
    xf(c, c->ux, c->uy, &X0, &Y0);
    xf(c, x1, y1, &X1, &Y1);
    xf(c, x, y, &X2, &Y2);
    float ddx = X0 - 2 * X1 + X2, ddy = Y0 - 2 * Y1 + Y2;
    int n = segs_for(mcuvg_sqrt(ddx * ddx + ddy * ddy), 0.125f, c->tol);
    for (int i = 1; i < n; i++) {
        float t = (float)i / (float)n, u = 1 - t;
        put(c, u * u * X0 + 2 * u * t * X1 + t * t * X2, u * u * Y0 + 2 * u * t * Y1 + t * t * Y2);
    }
    put(c, X2, Y2);
    c->ux = x;
    c->uy = y;
}

void mcuvg_cubic(mcuvg_ctx *c, float x1, float y1, float x2, float y2, float x, float y)
{
    ensure_open(c);
    float X0, Y0, X1, Y1, X2, Y2, X3, Y3;
    xf(c, c->ux, c->uy, &X0, &Y0);
    xf(c, x1, y1, &X1, &Y1);
    xf(c, x2, y2, &X2, &Y2);
    xf(c, x, y, &X3, &Y3);
    float ax = X0 - 2 * X1 + X2, ay = Y0 - 2 * Y1 + Y2, bx = X1 - 2 * X2 + X3, by = Y1 - 2 * Y2 + Y3;
    float dd = fmax_(ax * ax + ay * ay, bx * bx + by * by);
    int n = segs_for(mcuvg_sqrt(dd), 0.75f, c->tol);
    for (int i = 1; i < n; i++) {
        float t = (float)i / (float)n, u = 1 - t;
        float a = u * u * u, b = 3 * u * u * t, d = 3 * u * t * t, e = t * t * t;
        put(c, a * X0 + b * X1 + d * X2 + e * X3, a * Y0 + b * Y1 + d * Y2 + e * Y3);
    }
    put(c, X3, Y3);
    c->ux = x;
    c->uy = y;
}

/* the SVG arc (endpoint form): its centre solved in user space (F.6.5 of SVG 1.1), then points along it */
void mcuvg_arc(mcuvg_ctx *c, float rx, float ry, float rot_deg, int large, int sweep, float x, float y)
{
    ensure_open(c);
    float x0 = c->ux, y0 = c->uy;
    rx = fabs_(rx);
    ry = fabs_(ry);
    if (rx == 0 || ry == 0 || (x0 == x && y0 == y)) { mcuvg_line(c, x, y); return; }
    float sp, cp;
    sincos_(rot_deg * (PI_F / 180.0f), &sp, &cp);
    float dx = (x0 - x) * 0.5f, dy = (y0 - y) * 0.5f;
    float x1p = cp * dx + sp * dy, y1p = -sp * dx + cp * dy;
    float lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry);
    if (lam > 1) { float s = mcuvg_sqrt(lam); rx *= s; ry *= s; }
    float num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p;
    float den = rx * rx * y1p * y1p + ry * ry * x1p * x1p;
    float co = den > 0 && num > 0 ? mcuvg_sqrt(num / den) : 0.0f;
    if ((large != 0) == (sweep != 0)) co = -co;
    float cxp = co * rx * y1p / ry, cyp = -co * ry * x1p / rx;
    float cx = cp * cxp - sp * cyp + (x0 + x) * 0.5f, cy = sp * cxp + cp * cyp + (y0 + y) * 0.5f;
    float ux = (x1p - cxp) / rx, uy = (y1p - cyp) / ry, vx = (-x1p - cxp) / rx, vy = (-y1p - cyp) / ry;
    float t1 = mcuvg_atan2(uy, ux);
    float dt = mcuvg_atan2(ux * vy - uy * vx, ux * vx + uy * vy);
    if (!sweep && dt > 0) dt -= 2 * PI_F;
    else if (sweep && dt < 0) dt += 2 * PI_F;
    /* steps: the device radius decides (the larger axis through the matrix) */
    float sa = mcuvg_sqrt(c->m.a * c->m.a + c->m.b * c->m.b), sb = mcuvg_sqrt(c->m.c * c->m.c + c->m.d * c->m.d);
    float r = fmax_(rx, ry) * fmax_(sa, sb);
    float step = r > c->tol ? 2.0f * mcuvg_sqrt(2.0f * c->tol / r) : PI_F / 2;
    if (step > PI_F / 4) step = PI_F / 4;
    int n = iceil(fabs_(dt) / step);
    if (n < 1) n = 1;
    if (n > 512) n = 512;
    /* the chords lie inside the curve by r (1 - cos(h / 2)): the vertices go out by half of it, so the polygon
       straddles the arc instead of shrinking the shape (a ring drawn at 0.2 px lost a fifth of a pixel all round) */
    float g = 1.0f + 0.5f * (1.0f - mcuvg_cos(0.5f * fabs_(dt) / (float)n));
    for (int i = 1; i < n; i++) {
        float s, k;
        sincos_(t1 + dt * (float)i / (float)n, &s, &k);
        float ex = rx * g * k, ey = ry * g * s;
        float X, Y;
        xf(c, cp * ex - sp * ey + cx, sp * ex + cp * ey + cy, &X, &Y);
        put(c, X, Y);
    }
    mcuvg_line(c, x, y);
}

void mcuvg_close(mcuvg_ctx *c)
{
    if (!c->open) return;
    end_sub(c, 1);
    c->ux = c->usx;
    c->uy = c->usy;
}

/* ---- edges and rows ------------------------------------------------------------------------------------------------- */

static int edge(mcuvg_ctx *c, float x0, float y0, float x1, float y1)
{
    if (y0 == y1) return 0;
    if (c->ne >= c->cap) return -1;
    mcuvg_edge *e = &c->e[c->ne++];
    if (c->ne > c->hw_e) c->hw_e = c->ne;
    e->x0 = x0; e->y0 = y0; e->x1 = x1; e->y1 = y1;
    return 0;
}

static inline float etop(const mcuvg_edge *e) { return e->y0 < e->y1 ? e->y0 : e->y1; }
static inline float ebot(const mcuvg_edge *e) { return e->y0 < e->y1 ? e->y1 : e->y0; }

static void sort_edges(mcuvg_edge *e, uint32_t n)                    /* shell sort by top, in place */
{
    static const uint16_t gaps[] = { 701, 301, 132, 57, 23, 10, 4, 1 };
    for (unsigned g = 0; g < sizeof gaps / sizeof gaps[0]; g++) {
        uint32_t gap = gaps[g];
        for (uint32_t i = gap; i < n; i++) {
            mcuvg_edge t = e[i];
            float k = etop(&t);
            uint32_t j = i;
            for (; j >= gap && etop(&e[j - gap]) > k; j -= gap) e[j] = e[j - gap];
            e[j] = t;
        }
    }
}

/* cells i0 .. i1 of the row were written: their bits in the row's bitmap (the sweep reads only those) */
static inline __attribute__((always_inline)) void mark(uint32_t *tb, int i0, int i1)
{
    int w0 = i0 >> 5, w1 = i1 >> 5;
    for (int w = w0; w <= w1; w++) {
        uint32_t m = ~0u;
        if (w == w0) m &= ~0u << (i0 & 31);
        if (w == w1) m &= ~0u >> (31 - (i1 & 31));
        tb[w] |= m;
    }
}

/* the signed area of one piece of edge inside the row, into its cells; x in the row's local units, 0 .. span */
static inline __attribute__((always_inline)) void cells(float *a, uint32_t *tb, float x, float xn, float d)
{
    float x0 = x < xn ? x : xn, x1 = x < xn ? xn : x;
    int x0i = ifloor(x0), x1i = iceil(x1);
    float x0f = (float)x0i;
    if (x1i <= x0i + 1) {
        float xmf = 0.5f * (x + xn) - x0f;
        a[x0i] += d - d * xmf;
        a[x0i + 1] += d * xmf;
        mark(tb, x0i, x0i + 1);
        return;
    }
    mark(tb, x0i, x1i);
    float s = 1.0f / (x1 - x0);
    float x0r = x0 - x0f;
    float a0 = 0.5f * s * (1 - x0r) * (1 - x0r);
    float x1r = x1 - (float)x1i + 1;
    float am = 0.5f * s * x1r * x1r;
    a[x0i] += d * a0;
    if (x1i == x0i + 2) {
        a[x0i + 1] += d * (1 - a0 - am);
    } else {
        float a1 = s * (1.5f - x0r);
        a[x0i + 1] += d * (a1 - a0);
        for (int xi = x0i + 2; xi < x1i - 1; xi++) a[xi] += d * s;
        float a2 = a1 + (float)(x1i - x0i - 3) * s;
        a[x1i - 1] += d * (1 - a2 - am);
    }
    a[x1i] += d * am;
}

/* the piece of edge e between the rows' lines fy0 and fy1 (fy1 = fy0 + 1), with the row's origin at ox and its span
   (cells 0 .. span) - left of the row the piece runs straight down cell 0, right of it it is out of sight */
static inline __attribute__((always_inline)) void add_edge(float *a, uint32_t *tb, const mcuvg_edge *e, float fy0,
                                                           float fy1, float ox, float span)
{
    float x0 = e->x0, y0 = e->y0, x1 = e->x1, y1 = e->y1, dir = 1.0f;
    if (y0 > y1) { float t = x0; x0 = x1; x1 = t; t = y0; y0 = y1; y1 = t; dir = -1.0f; }
    float ya = fmax_(y0, fy0), yb = fmin_(y1, fy1);
    if (yb <= ya) return;
    float dxdy = (x1 - x0) / (y1 - y0);
    float xa = x0 + (ya - y0) * dxdy - ox, xb = x0 + (yb - y0) * dxdy - ox;
    if ((xa < 0) != (xb < 0)) {                                       /* crosses the row's left end: split there */
        float ym = ya + (0 - xa) / (xb - xa) * (yb - ya);
        if (xa < 0) { cells(a, tb, 0, 0, (ym - ya) * dir); ya = ym; xa = 0; }
        else { cells(a, tb, 0, 0, (yb - ym) * dir); yb = ym; xb = 0; }
    } else if (xa < 0) {
        cells(a, tb, 0, 0, (yb - ya) * dir);
        return;
    }
    if (xa > span) xa = span;
    if (xb > span) xb = span;
    cells(a, tb, xa, xb, (yb - ya) * dir);
}

/* ---- paint ---------------------------------------------------------------------------------------------------------- */

static inline uint32_t premul(uint32_t argb)
{
    uint32_t a = argb >> 24, r = (argb >> 16) & 255u, g = (argb >> 8) & 255u, b = argb & 255u;
    r = (r * a + 127u) / 255u; g = (g * a + 127u) / 255u; b = (b * a + 127u) / 255u;
    return a << 24 | r << 16 | g << 8 | b;
}

/* 256 premultiplied colours along the gradient (interpolated premultiplied: no dark fringe between a colour and a
   transparent stop) */
static void lut_fill(uint32_t *lut, const mcuvg_paint *p)
{
    int n = p->n < 2 ? 2 : p->n > MCUVG_STOPS ? MCUVG_STOPS : p->n;
    int k = 0;
    for (int i = 0; i < 256; i++) {
        float t = (float)i / 255.0f;
        while (k < n - 2 && t > p->off[k + 1]) k++;
        float t0 = p->off[k], t1 = p->off[k + 1];
        float u = t1 > t0 ? (t - t0) / (t1 - t0) : (t >= t1 ? 1.0f : 0.0f);
        if (u < 0) u = 0;
        if (u > 1) u = 1;
        uint32_t a = premul(p->col[k]), b = premul(p->col[k + 1]), out = 0;
        if (t <= p->off[0]) b = a, u = 0;
        if (t >= p->off[n - 1]) a = b = premul(p->col[n - 1]);
        for (int s = 0; s < 32; s += 8) {
            float va = (float)((a >> s) & 255u), vb = (float)((b >> s) & 255u);
            out |= (uint32_t)(va + (vb - va) * u + 0.5f) << s;
        }
        lut[i] = out;
    }
}

struct pnt {
    int kind;
    uint32_t flat;                     /* premultiplied */
    float ga, gb, gc;                  /* linear: t = ga x + gb y + gc at a pixel's centre */
    float ua, ub, uc, ud, ue, uf;      /* radial: device -> the gradient's unit circle: u = ua x + uc y + ue,
                                          v = ub x + ud y + uf; t = |(u, v)| */
#if MCUVG_V2
    int ext;                           /* a spread or a focal point: drawn by out_run_mask, not version 1's loop */
    int spread, focal;
    float fx, fy, fr, fa, dr;          /* the focal circle in the unit circle's space; fa = |f|^2 - dr^2, dr = 1 - fr */
#endif
};

static int paint_setup(mcuvg_ctx *c, const mcuvg_paint *p, struct pnt *q)
{
    q->kind = p->kind;
#if MCUVG_V2
    q->ext = 0;                                    /* a flat paint returns below, before the gradient's fields */
#endif
    if (p->kind == MCUVG_FLAT) { q->flat = premul(p->color); return 0; }
    const mcuvg_mat *m = &c->m;
    float det = m->a * m->d - m->b * m->c;
    if (det == 0) return -1;
    float ia = m->d / det, ib = -m->b / det, ic = -m->c / det, id = m->a / det;
    float ie = -(ia * m->e + ic * m->f), iff = -(ib * m->e + id * m->f);
    lut_fill(lut_of(c), p);
#if MCUVG_V2
    q->spread = p->spread;
    q->focal = 0;
    q->ext = p->spread != MCUVG_PAD;
#endif
    if (p->kind == MCUVG_LINEAR) {
        float dx = p->x1 - p->x0, dy = p->y1 - p->y0, l2 = dx * dx + dy * dy;
        if (l2 == 0) { q->kind = MCUVG_FLAT; q->flat = lut_of(c)[255]; return 0; }
        float gx = dx / l2, gy = dy / l2;                          /* t = g . (u - p0), u = inv(v) */
        q->ga = gx * ia + gy * ib;
        q->gb = gx * ic + gy * id;
        q->gc = gx * (ie - p->x0) + gy * (iff - p->y0);
        return 0;
    }
    /* radial: device -> user (the inverse of m) -> the unit circle: (user - centre) turned back by rot, then divided by
       the radii (version 1: rot 0, both radii x1) - one affine map a pixel, as cheap as the circle alone */
    float rx = p->x1, ry = rx, cr = 1.0f, sr = 0.0f;
#if MCUVG_V2
    if (p->ry > 0) ry = p->ry;
    if (p->rot != 0.0f) sincos_(p->rot * (PI_F / 180.0f), &sr, &cr);
#endif
    if (!(rx > 0) || !(ry > 0)) { q->kind = MCUVG_FLAT; q->flat = lut_of(c)[255]; return 0; }
    float kx = 1.0f / rx, ky = 1.0f / ry, ex = ie - p->x0, ey = iff - p->y0;
    q->ua = (cr * ia + sr * ib) * kx; q->uc = (cr * ic + sr * id) * kx; q->ue = (cr * ex + sr * ey) * kx;
    q->ub = (cr * ib - sr * ia) * ky; q->ud = (cr * id - sr * ic) * ky; q->uf = (cr * ey - sr * ex) * ky;
#if MCUVG_V2
    if (p->focal) {
        float fx = p->fx - p->x0, fy = p->fy - p->y0;
        float u = (cr * fx + sr * fy) * kx, v = (cr * fy - sr * fx) * ky, fr = p->fr > 0 ? p->fr * kx : 0.0f;
        if (fr > 0.9f) fr = 0.9f;
        float lim = 0.995f * (1.0f - fr), l = mcuvg_sqrt(u * u + v * v);
        if (l > lim) { u *= lim / l; v *= lim / l; }               /* outside the circle: onto its edge (SVG 1.1) */
        q->focal = 1;
        q->fx = u; q->fy = v; q->fr = fr; q->dr = 1.0f - fr;
        q->fa = u * u + v * v - q->dr * q->dr;                      /* < 0: the focal circle is inside */
        q->ext = 1;
    }
#endif
    return 0;
}

static inline uint32_t scale_px(uint32_t p, uint32_t k)               /* k 0..256 */
{
    uint32_t rb = ((p & 0x00FF00FFu) * k >> 8) & 0x00FF00FFu, ag = (((p >> 8) & 0x00FF00FFu) * k) & 0xFF00FF00u;
    return rb | ag;
}

/* pixels x0 .. x1 - 1 of row y, all with coverage cv (1..256), painted "over" the target straight away: the sweep calls
   it for one edge pixel or for the whole run between two edges (the inside of a shape: one colour, no per-pixel sum) */
/* the gradient's colour at (x, y) from coefficients held in registers (a struct on the stack is a load each, ~10
   cycles on a core whose data memory is uncached); the same sums in the same order as before, so pixels do not change */
static inline __attribute__((always_inline)) uint32_t lut_at(const uint32_t *lut, float t)
{
    int i = (int)(t * 255.0f + 0.5f);
    if (i < 0) i = 0;
    if (i > 255) i = 255;
    return lut[i];
}

/* 8-bit channels to RGB565, rounded to the nearest level (exactly (v * 31 + 127) / 255 and (v * 63 + 127) / 255):
   truncating instead darkened every colour by up to 7 levels of 255 in red and blue, a hue drift in dark tones.
   always_inline: at -Os GCC kept it a call per pixel, with its spills a fifth of a picture's cycles (QEMU model, 06.11) */
static inline __attribute__((always_inline)) uint32_t rgb565(uint32_t r, uint32_t g, uint32_t b)
{
    return ((r * 249u + 1014u) >> 11) << 11 | ((g * 253u + 505u) >> 10) << 5 | ((b * 249u + 1014u) >> 11);
}
#define ARGB565(p) rgb565(((p) >> 16) & 255u, ((p) >> 8) & 255u, (p) & 255u)

#if MCUVG_V2
/* version 2: t brought into 0..1 by the spread; NO_T (a point the focal gradient does not reach) is transparent */
#define NO_T (-1e30f)
static inline __attribute__((always_inline, unused)) uint32_t lut_sp(const uint32_t *lut, float t, int spread)
{
    if (t == NO_T) return 0;
    if (spread == MCUVG_REPEAT) t -= (float)ifloor(t);
    else if (spread == MCUVG_REFLECT) {
        t = fabs_(t);
        int k = ifloor(t);
        t -= (float)k;
        if (k & 1) t = 1.0f - t;
    }
    return lut_at(lut, t);
}
/* the focal gradient's t at (u, v) of the unit circle: the largest t whose circle - centre f + t (0 - f), radius
   fr + t (1 - fr) - passes through the point; fa < 0 (the focal circle inside) makes it one root */
static inline __attribute__((always_inline, unused)) float focal_t(float u, float v, float fx, float fy, float fr, float fa, float dr)
{
    float qx = u - fx, qy = v - fy;
    float b = fr * dr - (qx * fx + qy * fy), c0 = qx * qx + qy * qy - fr * fr, disc = b * b - fa * c0;
    if (disc < 0) disc = 0;
    return (b - mcuvg_sqrt(disc)) / fa;
}
#endif

static void out_run(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int x1, int y, uint32_t cv);
#if MCUVG_V2
static void out_run_mask(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int x1, int y, uint32_t cv);
#endif

/* the run's pixels by target: one function each, so that each loop gets the registers to itself (one function with
   all four let a change in one spill another: the RGB565 gradient loop lost 20 % to a new RGBA fast path, 06.11) */
#define RUN_LOCALS \
    const int lin = q->kind == MCUVG_LINEAR; \
    const float ga = q->ga, gb = q->gb, gc = q->gc; \
    const float ua = q->ua, ub = q->ub, uc = q->uc, ud = q->ud, ue = q->ue, uf = q->uf; \
    uint8_t *rowp = (uint8_t *)c->px + (size_t)y * (size_t)c->stride; \
    uint32_t f = q->kind == MCUVG_FLAT ? (cv == 256u ? q->flat : scale_px(q->flat, cv)) : 0u; \
    int flat = q->kind == MCUVG_FLAT; \
    float fy = (float)y + 0.5f; \
    (void)lin; (void)ga; (void)gb; (void)gc; (void)ua; (void)ub; (void)uc; (void)ud; (void)ue; (void)uf; (void)rowp; (void)fy;
#define PAINT(X) (lin ? lut_at(lut, ga * (X) + gb * fy + gc) \
                      : lut_at(lut, mcuvg_sqrt((ua * (X) + uc * fy + ue) * (ua * (X) + uc * fy + ue) + \
                                               (ub * (X) + ud * fy + uf) * (ub * (X) + ud * fy + uf))))

static __attribute__((noinline)) void run565(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int n, int y, uint32_t cv)
{
    RUN_LOCALS
    uint16_t *d = (uint16_t *)rowp + x0;
    int sw = c->fmt == MCUVG_RGB565_SWAP;
    if (flat && (f >> 24) == 255u) {                 /* opaque: the colour once, then stores only */
        uint32_t o = ARGB565(f);
        uint16_t v = (uint16_t)(sw ? ((o & 255u) << 8) | (o >> 8) : o);
        for (int i = 0; i < n; i++) d[i] = v;
        return;
    }
    for (int i = 0; i < n; i++) {
        uint32_t s = flat ? f : scale_px(PAINT((float)(x0 + i) + 0.5f), cv), sa = s >> 24, k = 255u - sa;
        if (!sa) continue;
        if (sa == 255u) {
            uint32_t o = ARGB565(s);
            d[i] = (uint16_t)(sw ? ((o & 255u) << 8) | (o >> 8) : o);
            continue;
        }
        uint32_t v = d[i];
        if (sw) v = ((v & 255u) << 8) | (v >> 8);
        uint32_t r = (v >> 11) & 31u, g = (v >> 5) & 63u, b = v & 31u;
        r = (r << 3) | (r >> 2); g = (g << 2) | (g >> 4); b = (b << 3) | (b >> 2);
        r = ((s >> 16) & 255u) + (r * k + 127u) / 255u;
        g = ((s >> 8) & 255u) + (g * k + 127u) / 255u;
        b = (s & 255u) + (b * k + 127u) / 255u;
        v = rgb565(r, g, b);
        d[i] = (uint16_t)(sw ? ((v & 255u) << 8) | (v >> 8) : v);
    }
    return;
}

static __attribute__((noinline)) void run_rgba(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int n, int y, uint32_t cv)
{
    RUN_LOCALS
    uint8_t *d = rowp + (size_t)x0 * 4u;
    if (flat && (f >> 24) == 255u && !((uintptr_t)d & 3u)) {   /* opaque: one word a pixel, no blending */
        uint32_t v = 0xFF000000u | (f & 255u) << 16 | (f & 0xFF00u) | (f >> 16 & 255u);   /* bytes r, g, b, a */
        uint32_t *w4 = (uint32_t *)(void *)d;
        for (int i = 0; i < n; i++) w4[i] = v;
        return;
    }
    for (int i = 0; i < n; i++, d += 4) {
        uint32_t s = flat ? f : scale_px(PAINT((float)(x0 + i) + 0.5f), cv), sa = s >> 24, k = 255u - sa;
        if (sa == 255u) {
            d[0] = (uint8_t)(s >> 16); d[1] = (uint8_t)(s >> 8); d[2] = (uint8_t)s; d[3] = 255u;
            continue;
        }
        d[0] = (uint8_t)(((s >> 16) & 255u) + (d[0] * k + 127u) / 255u);
        d[1] = (uint8_t)(((s >> 8) & 255u) + (d[1] * k + 127u) / 255u);
        d[2] = (uint8_t)((s & 255u) + (d[2] * k + 127u) / 255u);
        d[3] = (uint8_t)(sa + (d[3] * k + 127u) / 255u);
    }
    return;
}

static __attribute__((noinline)) void run_a8(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int n, int y, uint32_t cv)
{
    RUN_LOCALS
    uint8_t *d = rowp + x0;
    if (flat && (f >> 24) == 255u) {                 /* opaque: a store, no blending */
        for (int i = 0; i < n; i++) d[i] = 255u;
        return;
    }
    for (int i = 0; i < n; i++) {
        uint32_t sa = (flat ? f : scale_px(PAINT((float)(x0 + i) + 0.5f), cv)) >> 24;
        d[i] = (uint8_t)(sa + (d[i] * (255u - sa) + 127u) / 255u);
    }
    return;
}

static __attribute__((noinline)) void run_span(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int n, int y, uint32_t cv)
{
    RUN_LOCALS
    if (!c->span) return;
    while (n > 0) {                                  /* through the span buffer, w pixels at most at a time */
        int k = n < c->w ? n : c->w;
        for (int i = 0; i < k; i++) c->pix[i] = flat ? f : scale_px(PAINT((float)(x0 + i) + 0.5f), cv);
        c->span(c->user, x0, y, k, c->pix);
        x0 += k;
        n -= k;
    }
    return;
}

static void out_run(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int x1, int y, uint32_t cv)
{
#if MCUVG_V2
    /* a clip or a gradient of version 2: their own path, so version 1's loops stay exactly as fast */
    if (q->ext || c->mask) { out_run_mask(c, q, lut, x0, x1, y, cv); return; }
#endif
    switch (c->fmt) {
    case MCUVG_RGB565:
    case MCUVG_RGB565_SWAP: run565(c, q, lut, x0, x1 - x0, y, cv); return;
    case MCUVG_RGBA8888: run_rgba(c, q, lut, x0, x1 - x0, y, cv); return;
    case MCUVG_A8: run_a8(c, q, lut, x0, x1 - x0, y, cv); return;
    default: run_span(c, q, lut, x0, x1 - x0, y, cv); return;
    }
}

#undef PAINT

/* the flat-colour RGB565 case (icons, an application's raster) without a call per pixel: inlined into the sweep, so
   the colour and the row stay in registers (a call with its spills costs more than the pixel on an uncached core) */
static inline __attribute__((always_inline)) void fill565(uint16_t *d, int n, uint32_t f, uint32_t cv, uint32_t v565,
                                                          int sw)
{
    if (cv == 256u && (f >> 24) == 255u) {
        /* two pixels a store: the target is uncached PSRAM, a store costs ~8 cycles whatever its width */
        if (n > 0 && ((uintptr_t)d & 2u)) { *d++ = (uint16_t)v565; n--; }
        uint32_t two = v565 | v565 << 16, *d2 = (uint32_t *)(void *)d;
        for (int i = 0; i < (n >> 1); i++) d2[i] = two;
        if (n & 1) d[n - 1] = (uint16_t)v565;
        return;
    }
    uint32_t s = cv == 256u ? f : scale_px(f, cv), sa = s >> 24, k = 255u - sa;
    if (!sa) return;
    uint32_t sr = (s >> 16) & 255u, sg = (s >> 8) & 255u, sb = s & 255u;
    for (int i = 0; i < n; i++) {
        uint32_t v = d[i];
        if (sw) v = ((v & 255u) << 8) | (v >> 8);
        uint32_t r = (v >> 11) & 31u, g = (v >> 5) & 63u, b = v & 31u;
        r = (r << 3) | (r >> 2); g = (g << 2) | (g >> 4); b = (b << 3) | (b >> 2);
        r = sr + (r * k + 127u) / 255u;
        g = sg + (g * k + 127u) / 255u;
        b = sb + (b * k + 127u) / 255u;
        v = rgb565(r, g, b);
        d[i] = (uint16_t)(sw ? ((v & 255u) << 8) | (v >> 8) : v);
    }
}

static inline __attribute__((always_inline)) uint32_t cov_of(float acc, int rule)   /* a call each pixel otherwise */
{
    float v = fabs_(acc);
    if (rule == MCUVG_EVENODD) { v -= 2.0f * (float)ifloor(v * 0.5f); if (v > 1) v = 2 - v; }
    else if (v > 1) v = 1;
    return (uint32_t)(v * 256.0f + 0.5f);
}

/* what a row needs, set once per fill: the row's accumulator and bitmap, its width, the flat colour (RGB565 ready),
   and for the other targets the context, the paint and the gradient's table.  The row's work is split into small
   functions that load these into registers once per row: one big loop held them all and spilled ~25 of them to the
   stack, a load or a store per pixel at ~10 cycles each on an uncached core */
struct sweep {
    float *a;
    uint32_t *tb;
    int nw, span;
    uint32_t fl, v565;
    int sw;
    mcuvg_ctx *c;
    const struct pnt *q;
    const uint32_t *lut;
    int xa;
};

/* the active edges' pieces in the row [fy0, fy0 + 1) into its cells; the edges finished above it leave the list */
static __attribute__((noinline)) uint32_t accumulate(const mcuvg_edge *E, uint16_t *act, uint32_t nact,
                                                     const struct sweep *r, float fy0)
{
    float *a = r->a;
    uint32_t *tb = r->tb;
    float ox = (float)r->xa, span = (float)r->span, fy1 = fy0 + 1.0f;
    uint32_t k = 0;
    for (uint32_t i = 0; i < nact; i++) {
        const mcuvg_edge *e = &E[act[i]];
        if (ebot(e) <= fy0) continue;
        act[k++] = act[i];
        add_edge(a, tb, e, fy0, fy1, ox, span);
    }
    return k;
}

/* the sweep over the touched cells only: between two of them the sum - the coverage - does not change, so that run
   is painted in one go (the per-pixel sum and its clearing were a third of the time) */
static inline __attribute__((always_inline)) void sweep565(const struct sweep *r, uint16_t *d, int rule)
{
    float *a = r->a;
    uint32_t *tb = r->tb;
    const int nw = r->nw, span = r->span, sw = r->sw;
    const uint32_t fl = r->fl, v565 = r->v565;
    float acc = 0;
    int x = 0;
    for (int w = 0; w < nw; w++) {
        uint32_t bits = tb[w];
        if (!bits) continue;
        tb[w] = 0;
        while (bits) {
            int i = (w << 5) + __builtin_ctz(bits);
            bits &= bits - 1u;
            if (x < span && i > x) {
                uint32_t cv = cov_of(acc, rule);
                if (cv) fill565(d + x, (i < span ? i : span) - x, fl, cv, v565, sw);
            }
            acc += a[i];
            a[i] = 0;
            if (i < span) {
                uint32_t cv = cov_of(acc, rule);
                if (cv) fill565(d + i, 1, fl, cv, v565, sw);
            }
            x = i + 1;
        }
    }
    if (x < span) {
        uint32_t cv = cov_of(acc, rule);
        if (cv) fill565(d + x, span - x, fl, cv, v565, sw);
    }
}
/* noinline: called once each, -Os would fold them back into raster() and its spills */
static __attribute__((noinline)) void sweep565_nz(const struct sweep *r, uint16_t *d) { sweep565(r, d, MCUVG_NONZERO); }
static __attribute__((noinline)) void sweep565_eo(const struct sweep *r, uint16_t *d) { sweep565(r, d, MCUVG_EVENODD); }

/* the same for every other target and paint, through out_run */
static __attribute__((noinline)) void sweep_any(const struct sweep *r, int y, int rule)
{
    float *a = r->a;
    uint32_t *tb = r->tb;
    const int nw = r->nw, span = r->span, xa = r->xa;
    float acc = 0;
    int x = 0;
    for (int w = 0; w < nw; w++) {
        uint32_t bits = tb[w];
        if (!bits) continue;
        tb[w] = 0;
        while (bits) {
            int i = (w << 5) + __builtin_ctz(bits);
            bits &= bits - 1u;
            if (x < span && i > x) {
                uint32_t cv = cov_of(acc, rule);
                if (cv) out_run(r->c, r->q, r->lut, xa + x, xa + (i < span ? i : span), y, cv);
            }
            acc += a[i];
            a[i] = 0;
            if (i < span) {
                uint32_t cv = cov_of(acc, rule);
                if (cv) out_run(r->c, r->q, r->lut, xa + i, xa + i + 1, y, cv);
            }
            x = i + 1;
        }
    }
    if (x < span) {
        uint32_t cv = cov_of(acc, rule);
        if (cv) out_run(r->c, r->q, r->lut, xa + x, xa + span, y, cv);
    }
}

/* fill the edges in c->e with paint q under rule */
static int raster(mcuvg_ctx *c, const struct pnt *q, int rule)
{
    if (!c->ne) return MCUVG_OK;
    float ymin = 1e30f, ymax = -1e30f, xmin = 1e30f, xmax = -1e30f;
    for (uint32_t i = 0; i < c->ne; i++) {
        const mcuvg_edge *e = &c->e[i];
        ymin = fmin_(ymin, fmin_(e->y0, e->y1));
        ymax = fmax_(ymax, fmax_(e->y0, e->y1));
        xmin = fmin_(xmin, fmin_(e->x0, e->x1));
        xmax = fmax_(xmax, fmax_(e->x0, e->x1));
    }
    int y0 = ifloor(ymin), y1 = iceil(ymax), xa = ifloor(xmin), xb = iceil(xmax);
    if (y0 < c->cy0) y0 = c->cy0;
    if (y1 > c->cy1) y1 = c->cy1;
    if (xa < c->cx0) xa = c->cx0;
    if (xb > c->cx1) xb = c->cx1;
    if (y0 >= y1 || xa >= xb) return MCUVG_OK;
    sort_edges(c->e, c->ne);
    struct sweep r = { c->row, c->tb, (xb - xa + 2 + 31) >> 5, xb - xa, q->flat, 0, c->fmt == MCUVG_RGB565_SWAP,
                       c, q, lut_of(c), xa };
    uint32_t o565 = ARGB565(r.fl);
    r.v565 = r.sw ? ((o565 & 255u) << 8) | (o565 >> 8) : o565;
#if MCUVG_V2
    const int f565 = q->kind == MCUVG_FLAT && (c->fmt == MCUVG_RGB565 || r.sw) && !c->mask;   /* a clip: per pixel */
#else
    const int f565 = q->kind == MCUVG_FLAT && (c->fmt == MCUVG_RGB565 || r.sw);
#endif
    const mcuvg_edge *E = c->e;
    uint16_t *act = c->act;
    const uint32_t ne = c->ne;
    uint32_t next = 0, nact = 0;
    for (int y = y0; y < y1; y++) {
        float fy0 = (float)y;
        while (next < ne && etop(&E[next]) < fy0 + 1.0f) act[nact++] = (uint16_t)next++;
        nact = accumulate(E, act, nact, &r, fy0);
        if (f565) {
            uint16_t *d = (uint16_t *)(void *)((uint8_t *)c->px + (size_t)y * (size_t)c->stride) + xa;
            if (rule == MCUVG_EVENODD) sweep565_eo(&r, d); else sweep565_nz(&r, d);
        } else {
            sweep_any(&r, y, rule);
        }
    }
    return MCUVG_OK;
}

/* ---- fill ----------------------------------------------------------------------------------------------------------- */

int mcuvg_fill(mcuvg_ctx *c, const mcuvg_paint *p, int rule)
{
    if (c->full) return MCUVG_E_FULL;
    struct pnt q;
    if (paint_setup(c, p, &q)) return MCUVG_OK;
    c->ne = 0;
    uint32_t i = 0;
    while (i < c->npt) {
        uint32_t s = i;
        while (i < c->npt && c->pt[2u * i] != MARK) i++;
        uint32_t e = i;                                                /* points s .. e - 1 */
        if (e - s >= 2) {
            for (uint32_t k = s; k + 1 < e; k++)
                if (edge(c, c->pt[2 * k], c->pt[2 * k + 1], c->pt[2 * k + 2], c->pt[2 * k + 3])) return MCUVG_E_FULL;
            if (edge(c, c->pt[2 * (e - 1)], c->pt[2 * (e - 1) + 1], c->pt[2 * s], c->pt[2 * s + 1])) return MCUVG_E_FULL;
        }
        if (i < c->npt) i++;                                           /* the marker */
    }
    return raster(c, &q, rule);
}

/* ---- stroke --------------------------------------------------------------------------------------------------------- */

struct ol { mcuvg_ctx *c; float fx, fy, lx, ly; int n, err; };      /* an outline being drawn as edges */
static void ol_to(struct ol *o, float x, float y)
{
    if (o->n == 0) { o->fx = o->lx = x; o->fy = o->ly = y; o->n = 1; return; }
    if (x == o->lx && y == o->ly) return;
    if (edge(o->c, o->lx, o->ly, x, y)) o->err = 1;
    o->lx = x; o->ly = y; o->n++;
}
static void ol_close(struct ol *o)
{
    if (o->n > 1 && edge(o->c, o->lx, o->ly, o->fx, o->fy)) o->err = 1;
    o->n = 0;
}

/* points on the circle (cx, cy, r) from direction (ux, uy) turning by `ang` radians (sign = direction), the first
   point excluded, the last included */
static void ol_arc(struct ol *o, float cx, float cy, float r, float ux, float uy, float ang, float tol)
{
    float step = r > tol ? 2.0f * mcuvg_sqrt(2.0f * tol / r) : PI_F / 2;
    if (step > PI_F / 4) step = PI_F / 4;
    int n = iceil(fabs_(ang) / step);
    if (n < 1) n = 1;
    float s, k;
    sincos_(ang / (float)n, &s, &k);
    float g = r * (1.0f + 0.5f * (1.0f - mcuvg_cos(0.5f * fabs_(ang) / (float)n)));   /* as in mcuvg_arc */
    float x = ux, y = uy;
    for (int i = 0; i < n; i++) {
        float nx = x * k - y * s, ny = x * s + y * k;
        x = nx; y = ny;
        float rr = i == n - 1 ? r : g;                                 /* the last point exactly on the circle */
        ol_to(o, cx + rr * x, cy + rr * y);
    }
}

struct stk { float hw, tol, miter; int cap, join; };

/* one side of the polyline p[0..n-1] (device points): the offsets along its LEFT, with joins; closed: the join at
   p[0] as well, open: from the first point's offset to the last's */
static void side(struct ol *o, const float *p, int n, int step, int closed, const struct stk *k)
{
    /* p is read as p[i * step]: step 2 forwards, -2 backwards (p then points at the last point) */
#define PX(i) p[(i) * step]
#define PY(i) p[(i) * step + 1]
    int segs = closed ? n : n - 1;
    for (int i = 0; i < segs; i++) {
        int a = i, b = (i + 1) % n;
        float dx = PX(b) - PX(a), dy = PY(b) - PY(a), l = mcuvg_sqrt(dx * dx + dy * dy);
        if (l == 0) continue;
        dx /= l; dy /= l;
        float nx = -dy * k->hw, ny = dx * k->hw;                     /* the left offset */
        if (i > 0 || closed) {
            /* the join at a: from the previous segment's offset end to this one's start */
            int z = (a - 1 + n) % n;
            float pdx = PX(a) - PX(z), pdy = PY(a) - PY(z), pl = mcuvg_sqrt(pdx * pdx + pdy * pdy);
            if (pl > 0) {
                pdx /= pl; pdy /= pl;
                float pnx = -pdy * k->hw, pny = pdx * k->hw;
                float cr = pdx * dy - pdy * dx, dt = pdx * dx + pdy * dy;
                float ax = PX(a) + pnx, ay = PY(a) + pny, bx = PX(a) + nx, by = PY(a) + ny;
                if (cr > 1e-6f) {                                       /* turning left: the left side is inside */
                    ol_to(o, ax, ay);
                    ol_to(o, PX(a), PY(a));
                    ol_to(o, bx, by);
                } else if (cr < -1e-6f || dt < 0) {                    /* the outer side */
                    ol_to(o, ax, ay);
                    if (k->join == MCUVG_JOIN_ROUND) {
                        ol_arc(o, PX(a), PY(a), k->hw, pnx / k->hw, pny / k->hw, mcuvg_atan2(cr, dt), k->tol);
                    } else if (k->join == MCUVG_MITER) {
                        float ux = pnx + nx, uy = pny + ny;             /* hw (n1 + n2) */
                        float cosv = (pnx * nx + pny * ny) / (k->hw * k->hw);
                        if (1 + cosv > 1e-6f && 2.0f / (1 + cosv) <= k->miter * k->miter) {
                            float f = 1.0f / (1 + cosv);
                            ol_to(o, PX(a) + ux * f, PY(a) + uy * f);
                        }
                    }
                    ol_to(o, bx, by);
                } else {
                    ol_to(o, bx, by);
                }
            } else {
                ol_to(o, PX(a) + nx, PY(a) + ny);
            }
        } else {
            ol_to(o, PX(a) + nx, PY(a) + ny);
        }
        ol_to(o, PX(b) + nx, PY(b) + ny);
    }
#undef PX
#undef PY
}

/* the cap at the polyline's end point (x, y) arriving with unit direction (dx, dy): from the left offset round to
   the right one */
static void cap_at(struct ol *o, float x, float y, float dx, float dy, const struct stk *k)
{
    float nx = -dy * k->hw, ny = dx * k->hw;
    if (k->cap == MCUVG_SQUARE) {
        ol_to(o, x + nx + dx * k->hw, y + ny + dy * k->hw);
        ol_to(o, x - nx + dx * k->hw, y - ny + dy * k->hw);
    } else if (k->cap == MCUVG_ROUND) {
        ol_arc(o, x, y, k->hw, -dy, dx, -PI_F, k->tol);
        return;
    }
    ol_to(o, x - nx, y - ny);
}

/* a dot - a subpath of one point or a zero-length piece: drawn by the round and the square caps only (as SVG does) */
static void dot(struct ol *o, float x, float y, const struct stk *k)
{
    if (k->cap == MCUVG_ROUND) {
        ol_to(o, x + k->hw, y);
        ol_arc(o, x, y, k->hw, 1, 0, 2 * PI_F, k->tol);
        ol_close(o);
    } else if (k->cap == MCUVG_SQUARE) {
        ol_to(o, x - k->hw, y - k->hw); ol_to(o, x + k->hw, y - k->hw);
        ol_to(o, x + k->hw, y + k->hw); ol_to(o, x - k->hw, y + k->hw);
        ol_close(o);
    }
}

/* an open polyline pp[0..n-1], n >= 2: one side, the end cap, the other side back, the start cap */
static void open_line(struct ol *o, const float *pp, int n, const struct stk *k)
{
    const float *last = pp + 2 * (n - 1);
    float dx = last[0] - last[-2], dy = last[1] - last[-1], l = mcuvg_sqrt(dx * dx + dy * dy);
    float sx = pp[0] - pp[2], sy = pp[1] - pp[3], sl = mcuvg_sqrt(sx * sx + sy * sy);
    side(o, pp, n, 2, 0, k);
    if (l > 0) cap_at(o, last[0], last[1], dx / l, dy / l, k);
    side(o, last, n, -2, 0, k);
    if (sl > 0) cap_at(o, pp[0], pp[1], sx / sl, sy / sl, k);
    ol_close(o);
}

#if MCUVG_V2
/* version 2: the dashes of one subpath pp[0..n-1] (device points; closed: its last point joins the first).  Each dash
   is built in the free tail of the point store and outlined as an open line; the pattern runs on across vertices and
   subpaths' corners as SVG's does.  -1: the tail is too short for a dash */
static int dashes(mcuvg_ctx *c, struct ol *o, const float *pp, int n, int closed, const struct stk *k)
{
    float d[MCUVG_DASHES], total = 0, scale = mat_scale(&c->m);
    int nd = c->ndash;
    for (int i = 0; i < nd; i++) { d[i] = c->dash[i] * scale; total += d[i]; }
    if (!(total > 0)) return 1;                                         /* no pattern: the caller draws it solid */
    float *buf = c->pt + 2u * c->npt;
    uint32_t room = c->cappt - c->npt, np = 0;
    float pos = c->dash_off * scale;
    pos -= total * (float)ifloor(pos / total);
    int idx = 0;
    while (pos > 0 && pos >= d[idx]) { pos -= d[idx]; idx = (idx + 1) % nd; }
    float rem = d[idx] - pos;
    int segs = closed ? n : n - 1;
    for (int i = 0; i < segs; i++) {
        float ax = pp[2 * i], ay = pp[2 * i + 1];
        const float *b = pp + 2 * ((i + 1) % n);
        float dx = b[0] - ax, dy = b[1] - ay, L = mcuvg_sqrt(dx * dx + dy * dy), t = 0;
        if (L == 0) continue;
        dx /= L; dy /= L;
        for (;;) {
            if (rem <= 1e-6f) {                                         /* an element ends here, or is empty */
                if (!(idx & 1)) {                                       /* a dash: out with it; an empty one is a dot */
                    if (np >= 2 && (buf[0] != buf[2 * np - 2] || buf[1] != buf[2 * np - 1])) open_line(o, buf, (int)np, k);
                    else dot(o, np ? buf[0] : ax + dx * t, np ? buf[1] : ay + dy * t, k);
                    np = 0;
                }
                idx = (idx + 1) % nd;                                   /* total > 0: a long element comes */
                rem = d[idx];
                continue;
            }
            if (t >= L) break;
            float step = rem < L - t ? rem : L - t;
            if (!(idx & 1)) {                                           /* inside a dash */
                if (np == 0) {
                    if (room < 2) return -1;
                    buf[0] = ax + dx * t; buf[1] = ay + dy * t; np = 1;
                }
                if (np >= room) return -1;
                buf[2 * np] = ax + dx * (t + step); buf[2 * np + 1] = ay + dy * (t + step); np++;
            }
            t += step;
            rem -= step;
        }
    }
    if (np >= 2) open_line(o, buf, (int)np, k);
    else if (np == 1) dot(o, buf[0], buf[1], k);
    return 0;
}
#endif

int mcuvg_stroke(mcuvg_ctx *c, const mcuvg_paint *p, float width, int cap, int join, float miter_limit)
{
    if (c->full) return MCUVG_E_FULL;
    struct pnt q;
    if (width <= 0 || paint_setup(c, p, &q)) return MCUVG_OK;
    struct stk k = { 0.5f * width * mat_scale(&c->m), c->tol, miter_limit < 1 ? 1 : miter_limit, cap, join };
    if (k.hw <= 0) return MCUVG_OK;
    struct ol o = { c, 0, 0, 0, 0, 0, 0 };
    c->ne = 0;
    uint32_t i = 0;
    while (i < c->npt) {
        uint32_t s = i;
        while (i < c->npt && c->pt[2u * i] != MARK) i++;
        int n = (int)(i - s), closed = i < c->npt && c->pt[2u * i + 1u] != 0.0f;
        const float *pp = c->pt + 2u * s;
        if (closed && n > 2 && pp[0] == pp[2 * (n - 1)] && pp[1] == pp[2 * (n - 1) + 1]) n--;   /* "L start Z" */
#if MCUVG_V2
        int dr = c->ndash && n >= 2 ? dashes(c, &o, pp, n, closed, &k) : 1;
        if (dr < 0) return MCUVG_E_FULL;
        if (dr == 0) { if (o.err) return MCUVG_E_FULL; if (i < c->npt) i++; continue; }
#endif
        if (n == 1 || (n == 2 && pp[0] == pp[2] && pp[1] == pp[3])) {
            dot(&o, pp[0], pp[1], &k);
        } else if (n >= 2) {
            if (closed) {
                const float *last = pp + 2 * (n - 1);
                side(&o, pp, n, 2, 1, &k);
                ol_close(&o);
                side(&o, last, n, -2, 1, &k);
                ol_close(&o);
            } else {
                open_line(&o, pp, n, &k);
            }
        }
        if (o.err) return MCUVG_E_FULL;
        if (i < c->npt) i++;
    }
    if (o.err) return MCUVG_E_FULL;
    return raster(c, &q, MCUVG_NONZERO);
}

#if MCUVG_V2
/* ==== version 2: clips, groups, shadows, blur ========================================================================
   Coordinates: a layer is a window onto the first target (the one given to mcuvg_init); c->ox, c->oy is the corner of
   the target being drawn into, in the first target's pixels, so masks and layers line up whatever the nesting.  Every
   box is clipped to the target it is drawn from, so a layer is never wider than the first target and the row buffers
   made for it serve every layer. */

void mcuvg_layers(mcuvg_ctx *c, void *mem, size_t len)
{
    uint8_t *p = (uint8_t *)(((uintptr_t)mem + 7u) & ~(uintptr_t)7u);
    size_t skip = (size_t)(p - (uint8_t *)mem);
    c->lm = mem && len > skip ? p : 0;
    c->llen = mem && len > skip ? len - skip : 0;
    c->lused = 0;
    c->lhw = 0;
}

size_t mcuvg_layers_used(const mcuvg_ctx *c) { return c->lhw; }

static uint8_t *lalloc(mcuvg_ctx *c, size_t n)            /* zeroed */
{
    n = align8(n);
    if (!c->lm || n > c->llen - c->lused) return 0;
    uint8_t *p = c->lm + c->lused;
    c->lused += n;
    if (c->lused > c->lhw) c->lhw = c->lused;
    uint32_t *w = (uint32_t *)(void *)p;
    for (size_t i = 0; i < n / 4u; i++) w[i] = 0;
    return p;
}

int mcuvg_dash(mcuvg_ctx *c, const float *len, int n, float offset)
{
    c->ndash = 0;
    c->dash_off = offset;
    if (!len || n <= 0) return MCUVG_OK;
    int m = n & 1 ? 2 * n : n;                                     /* an odd list is repeated (SVG) */
    if (m > MCUVG_DASHES) return MCUVG_E_ARG;
    float total = 0;
    for (int i = 0; i < m; i++) {
        float v = len[i % n];
        if (v < 0) return MCUVG_E_ARG;
        c->dash[i] = v;
        total += v;
    }
    if (total > 0) c->ndash = m;
    return MCUVG_OK;
}

/* the 8-bit mask in force at the first target's pixel (X, Y): 255 outside every clip */
static inline uint32_t mask_at(const mcuvg_ctx *c, int X, int Y)
{
    int u = X - c->mx0, v = Y - c->my0;
    return u >= 0 && v >= 0 && u < c->mw && v < c->mh ? c->mask[v * c->mw + u] : 0u;
}

/* n premultiplied pixels "over" row y of the target from x0 (the target's own pixels) */
static void blend_row(mcuvg_ctx *c, int x0, int y, int n, const uint32_t *s)
{
    uint8_t *rowp = (uint8_t *)c->px + (size_t)y * (size_t)c->stride;
    switch (c->fmt) {
    case MCUVG_RGB565:
    case MCUVG_RGB565_SWAP: {
        uint16_t *d = (uint16_t *)(void *)rowp + x0;
        int sw = c->fmt == MCUVG_RGB565_SWAP;
        for (int i = 0; i < n; i++) {
            uint32_t p = s[i], sa = p >> 24, k = 255u - sa;
            if (!sa) continue;
            uint32_t v = d[i];
            if (sw) v = ((v & 255u) << 8) | (v >> 8);
            uint32_t r = (v >> 11) & 31u, g = (v >> 5) & 63u, b = v & 31u;
            r = (r << 3) | (r >> 2); g = (g << 2) | (g >> 4); b = (b << 3) | (b >> 2);
            r = ((p >> 16) & 255u) + (r * k + 127u) / 255u;
            g = ((p >> 8) & 255u) + (g * k + 127u) / 255u;
            b = (p & 255u) + (b * k + 127u) / 255u;
            v = rgb565(r > 255u ? 255u : r, g > 255u ? 255u : g, b > 255u ? 255u : b);
            d[i] = (uint16_t)(sw ? ((v & 255u) << 8) | (v >> 8) : v);
        }
        return;
    }
    case MCUVG_RGBA8888: {
        uint8_t *d = rowp + (size_t)x0 * 4u;
        for (int i = 0; i < n; i++, d += 4) {
            uint32_t p = s[i], sa = p >> 24, k = 255u - sa;
            if (!sa) continue;
            d[0] = (uint8_t)(((p >> 16) & 255u) + (d[0] * k + 127u) / 255u);
            d[1] = (uint8_t)(((p >> 8) & 255u) + (d[1] * k + 127u) / 255u);
            d[2] = (uint8_t)((p & 255u) + (d[2] * k + 127u) / 255u);
            d[3] = (uint8_t)(sa + (d[3] * k + 127u) / 255u);
        }
        return;
    }
    case MCUVG_A8: {
        uint8_t *d = rowp + x0;
        for (int i = 0; i < n; i++) {
            uint32_t sa = s[i] >> 24;
            d[i] = (uint8_t)(sa + (d[i] * (255u - sa) + 127u) / 255u);
        }
        return;
    }
    default:
        if (c->span) c->span(c->user, x0, y, n, s);
        return;
    }
}

/* a gradient run under a clip or of version 2 (spread, focal point): its coefficients held in registers (a load of the
   paint's fields a pixel would cost ~10 cycles each here), the coverage times the mask's row when mrow is given */
static __attribute__((noinline)) void run_grad(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int x1, int y,
                                               uint32_t cv, const uint8_t *mrow)
{
    const int lin = q->kind == MCUVG_LINEAR, spread = q->spread, focal = q->focal;
    const float ga = q->ga, gb = q->gb, gc = q->gc, ua = q->ua, ub = q->ub, uc = q->uc, ud = q->ud, ue = q->ue, uf = q->uf;
    const float ffx = q->fx, ffy = q->fy, ffr = q->fr, ffa = q->fa, fdr = q->dr, fy = (float)y + 0.5f;
    while (x0 < x1) {
        int n = x1 - x0 < c->w ? x1 - x0 : c->w;
        for (int i = 0; i < n; i++) {
            uint32_t k = cv;
            if (mrow) {
                uint32_t m = mrow[x0 + i];
                k = m ? (cv * m + 127u) / 255u : 0u;
                if (!k) { c->pix[i] = 0; continue; }
            }
            float X = (float)(x0 + i) + 0.5f, t;
            if (lin) t = ga * X + gb * fy + gc;
            else {
                float u = ua * X + uc * fy + ue, v = ub * X + ud * fy + uf;
                t = focal ? focal_t(u, v, ffx, ffy, ffr, ffa, fdr) : mcuvg_sqrt(u * u + v * v);
            }
            uint32_t p = lut_sp(lut, t, spread);
            c->pix[i] = k == 256u ? p : scale_px(p, k);
        }
        blend_row(c, x0, y, n, c->pix);
        x0 += n;
    }
}

/* out_run under a clip, or with a gradient of version 2: a flat paint once, a gradient by run_grad; the coverage times
   the mask's row (looked up once a run), then over the target */
static void out_run_mask(mcuvg_ctx *c, const struct pnt *q, const uint32_t *lut, int x0, int x1, int y, uint32_t cv)
{
    const uint8_t *mrow = 0;
    if (c->mask) {
        int Y = y + c->oy, mx = c->mx0 - c->ox;                  /* the mask's first column, in the target's pixels */
        if (Y < c->my0 || Y >= c->my0 + c->mh) return;           /* the row lies outside the clip */
        if (x0 < mx) x0 = mx;
        if (x1 > mx + c->mw) x1 = mx + c->mw;
        mrow = c->mask + (size_t)(Y - c->my0) * (size_t)c->mw - mx;
    }
    if (q->kind != MCUVG_FLAT) { run_grad(c, q, lut, x0, x1, y, cv, mrow); return; }
    const uint32_t flat = q->flat;
    while (x0 < x1) {
        int n = x1 - x0 < c->w ? x1 - x0 : c->w;
        for (int i = 0; i < n; i++) {
            uint32_t m = mrow[x0 + i], k = m ? (cv * m + 127u) / 255u : 0u;
            c->pix[i] = k ? scale_px(flat, k) : 0u;
        }
        blend_row(c, x0, y, n, c->pix);
        x0 += n;
    }
}

/* ---- boxes ---------------------------------------------------------------------------------------------------------- */

struct box { int x, y, w, h; };                            /* the first target's pixels */

/* the user box (x0, y0, x1, y1) through the matrix, grown by g pixels, inside the target's clip: empty w or h <= 0 */
static struct box dev_box(const mcuvg_ctx *c, float x0, float y0, float x1, float y1, float g)
{
    float X[4], Y[4];
    xf(c, x0, y0, &X[0], &Y[0]); xf(c, x1, y0, &X[1], &Y[1]);
    xf(c, x0, y1, &X[2], &Y[2]); xf(c, x1, y1, &X[3], &Y[3]);
    float a = X[0], b = X[0], e = Y[0], f = Y[0];
    for (int i = 1; i < 4; i++) { a = fmin_(a, X[i]); b = fmax_(b, X[i]); e = fmin_(e, Y[i]); f = fmax_(f, Y[i]); }
    int bx0 = ifloor(a - g) - 1, bx1 = iceil(b + g) + 1, by0 = ifloor(e - g) - 1, by1 = iceil(f + g) + 1;
    if (bx0 < c->cx0) bx0 = c->cx0;
    if (by0 < c->cy0) by0 = c->cy0;
    if (bx1 > c->cx1) bx1 = c->cx1;
    if (by1 > c->cy1) by1 = c->cy1;
    struct box r = { bx0 + c->ox, by0 + c->oy, bx1 - bx0, by1 - by0 };
    return r;
}

/* ---- the frames: a shadow pass or a group in progress ------------------------------------------------------------- */

static struct mcuvg_frame *push(mcuvg_ctx *c, int pass, const mcuvg_group *g)
{
    if (c->depth >= MCUVG_DEPTH) return 0;
    struct mcuvg_frame *f = &c->fr[c->depth++];
    f->px = c->px; f->w = c->w; f->h = c->h; f->stride = c->stride; f->fmt = c->fmt;
    f->cx0 = c->cx0; f->cy0 = c->cy0; f->cx1 = c->cx1; f->cy1 = c->cy1; f->ox = c->ox; f->oy = c->oy;
    f->span = c->span; f->user = c->user;
    f->mask = c->mask; f->mx0 = c->mx0; f->my0 = c->my0; f->mw = c->mw; f->mh = c->mh;
    f->m = c->m;
    f->lused = c->lused;
    f->layer = 0; f->lx = f->ly = f->lw = f->lh = f->lst = 0;
    f->pass = pass;
    if (g) f->g = *g;
    return f;
}

static void restore(mcuvg_ctx *c, const struct mcuvg_frame *f)   /* the target, clip, mask and matrix of before */
{
    c->px = f->px; c->w = f->w; c->h = f->h; c->stride = f->stride; c->fmt = f->fmt;
    c->cx0 = f->cx0; c->cy0 = f->cy0; c->cx1 = f->cx1; c->cy1 = f->cy1; c->ox = f->ox; c->oy = f->oy;
    c->span = f->span; c->user = f->user;
    c->mask = f->mask; c->mx0 = f->mx0; c->my0 = f->my0; c->mw = f->mw; c->mh = f->mh;
    c->m = f->m;
}

/* draw into the layer buf (box b, bpp bytes a pixel) from now on: the matrix moved by the layer's corner */
static void retarget(mcuvg_ctx *c, uint8_t *buf, const struct box *b, int fmt, int bpp)
{
    c->m.e -= (float)(b->x - c->ox);
    c->m.f -= (float)(b->y - c->oy);
    c->px = buf; c->w = b->w; c->h = b->h; c->stride = b->w * bpp; c->fmt = fmt;
    c->cx0 = 0; c->cy0 = 0; c->cx1 = b->w; c->cy1 = b->h; c->ox = b->x; c->oy = b->y;
    c->span = 0; c->user = 0;
}

static void draw_nothing(mcuvg_ctx *c) { c->cx1 = c->cx0; c->cy1 = c->cy0; }

/* ---- blur: three box blurs, each a running sum (the cost does not grow with the radius) --------------------------- */

static void box_radii(float sigma, int r[3])
{
    float wi = mcuvg_sqrt(4.0f * sigma * sigma + 1.0f);         /* sqrt(12 s^2 / 3 + 1) */
    int wl = (int)wi;
    if (!(wl & 1)) wl--;
    if (wl < 1) wl = 1;
    float mi = (12.0f * sigma * sigma - 3.0f * (float)(wl * wl) - 12.0f * (float)wl - 9.0f) / (-4.0f * (float)wl - 4.0f);
    int m = (int)(mi + 0.5f);
    for (int i = 0; i < 3; i++) r[i] = ((i < m ? wl : wl + 2) - 1) / 2;
}

/* One box pass over n words of four 8-bit lanes each: dst[x] = the mean of src[x - r .. x + r] (0 outside), lane by
   lane.  Lanes 0 and 2 and lanes 1 and 3 are summed together, 16 bits each (r < 128: 255 (2r + 1) fits).  On a core
   whose data memory is uncached a load or a store costs ~10 cycles, so the four lanes of a word are the point: an RGBA
   pixel's channels, or four rows or columns of an 8-bit layer side by side. */
static void box_lanes(const uint32_t *src, uint32_t *dst, int n, int r)
{
    uint32_t w = 2u * (uint32_t)r + 1u, inv = (65536u + w / 2u) / w, ev = 0, od = 0;
    for (int i = 0; i <= r && i < n; i++) { uint32_t v = src[i]; ev += v & 0x00FF00FFu; od += (v >> 8) & 0x00FF00FFu; }
    for (int x = 0; x < n; x++) {
        dst[x] = (((ev & 0xFFFFu) * inv + 32768u) >> 16) | ((((od & 0xFFFFu) * inv + 32768u) >> 16) << 8) |
                 ((((ev >> 16) * inv + 32768u) >> 16) << 16) | ((((od >> 16) * inv + 32768u) >> 16) << 24);
        if (x + r + 1 < n) { uint32_t v = src[x + r + 1]; ev += v & 0x00FF00FFu; od += (v >> 8) & 0x00FF00FFu; }
        if (x - r >= 0) { uint32_t v = src[x - r]; ev -= v & 0x00FF00FFu; od -= (v >> 8) & 0x00FF00FFu; }
    }
}

/* three box passes over n words: a -> b -> a -> b; the result in b */
static void box3(uint32_t *a, uint32_t *b, int n, const int r[3])
{
    box_lanes(a, b, n, r[0]);
    box_lanes(b, a, n, r[1]);
    box_lanes(a, b, n, r[2]);
}

/* four bytes of four words as four words of four bytes (a 4 x 4 transpose; its own inverse) */
static inline __attribute__((always_inline)) void tr4(uint32_t *a, uint32_t *b, uint32_t *c, uint32_t *d)
{
    uint32_t t0 = (*a & 0x00FF00FFu) | ((*b & 0x00FF00FFu) << 8), t1 = ((*a >> 8) & 0x00FF00FFu) | (*b & 0xFF00FF00u);
    uint32_t t2 = (*c & 0x00FF00FFu) | ((*d & 0x00FF00FFu) << 8), t3 = ((*c >> 8) & 0x00FF00FFu) | (*d & 0xFF00FF00u);
    *a = (t0 & 0xFFFFu) | (t2 << 16); *b = (t1 & 0xFFFFu) | (t3 << 16);
    *c = (t0 >> 16) | (t2 & 0xFFFF0000u); *d = (t1 >> 16) | (t3 & 0xFFFF0000u);
}

/* a Gaussian blur of sigma pixels, as three box blurs each way (the cost does not grow with the radius): an 8-bit
   buffer (bpp 1) or an RGBA one (bpp 4), w x h, `stride` bytes a row (a multiple of 4), 4-aligned; t0, t1: scratch of
   max(w, h) + 4 words each.  Outside the buffer is 0 (transparent), as SVG's blur sees it */
static void blur(uint8_t *buf, int w, int h, int stride, int bpp, float sigma, uint32_t *t0, uint32_t *t1)
{
    if (!(sigma > 0.2f)) return;
    if (sigma > 120.0f) sigma = 120.0f;
    int r[3];
    box_radii(sigma, r);
    if (bpp == 4) {
        for (int y = 0; y < h; y++) {                        /* rows: a pixel is a word of four channels */
            uint32_t *row = (uint32_t *)(void *)(buf + (size_t)y * (size_t)stride);
            box_lanes(row, t0, w, r[0]);
            box_lanes(t0, t1, w, r[1]);
            box_lanes(t1, row, w, r[2]);
        }
        for (int x = 0; x < w; x++) {                        /* columns: gathered, blurred, put back */
            uint32_t *col = (uint32_t *)(void *)(buf + (size_t)x * 4u);
            int sw = stride / 4;
            for (int y = 0; y < h; y++) t0[y] = col[y * sw];
            box3(t0, t1, h, r);
            for (int y = 0; y < h; y++) col[y * sw] = t1[y];
        }
        return;
    }
    int sw = stride / 4, nw = (w + 3) / 4;
    for (int y0 = 0; y0 < h; y0 += 4) {                      /* four rows at once: each column a word of four rows */
        uint32_t *rw[4];
        for (int k = 0; k < 4; k++) rw[k] = y0 + k < h ? (uint32_t *)(void *)(buf + (size_t)(y0 + k) * (size_t)stride) : 0;
        for (int i = 0; i < nw; i++) {
            uint32_t a = rw[0][i], b = rw[1] ? rw[1][i] : 0, c = rw[2] ? rw[2][i] : 0, d = rw[3] ? rw[3][i] : 0;
            tr4(&a, &b, &c, &d);
            t0[4 * i] = a; t0[4 * i + 1] = b; t0[4 * i + 2] = c; t0[4 * i + 3] = d;
        }
        box3(t0, t1, w, r);
        for (int i = 0; i < nw; i++) {
            uint32_t a = t1[4 * i], b = t1[4 * i + 1], c = t1[4 * i + 2], d = t1[4 * i + 3];
            tr4(&a, &b, &c, &d);
            for (int k = 0; k < 4; k++) if (rw[k]) rw[k][i] = k == 0 ? a : k == 1 ? b : k == 2 ? c : d;
        }
    }
    for (int i = 0; i < sw; i++) {                           /* four columns at once: a word of a row */
        uint32_t *col = (uint32_t *)(void *)buf + i;
        for (int y = 0; y < h; y++) t0[y] = col[y * sw];
        box3(t0, t1, h, r);
        for (int y = 0; y < h; y++) col[y * sw] = t1[y];
    }
}

/* ---- the shadow pass ------------------------------------------------------------------------------------------------ */

int mcuvg_shadow_begin(mcuvg_ctx *c, const mcuvg_group *g)
{
    if (!g || g->nshadow > MCUVG_SHADOWS) return MCUVG_E_ARG;
    struct mcuvg_frame *f = push(c, 1, g);
    if (!f) return MCUVG_E_FULL;
    c->mask = 0;                                    /* the clip in force applies when the shadow is laid, not here */
    struct box b = dev_box(c, g->x0, g->y0, g->x1, g->y1, 0);
    if (b.w <= 0 || b.h <= 0) { draw_nothing(c); return MCUVG_OK; }
    int st4 = (b.w + 3) & ~3;                           /* rows a multiple of 4 bytes: the blur reads them by words */
    uint8_t *buf = lalloc(c, (size_t)st4 * (size_t)b.h);
    if (!buf) { c->depth--; restore(c, f); return MCUVG_E_FULL; }
    f->layer = buf; f->lx = b.x; f->ly = b.y; f->lw = b.w; f->lh = b.h; f->lst = st4;
    retarget(c, buf, &b, MCUVG_A8, 1);              /* the group's alpha: everything it draws, as coverage */
    c->stride = st4;
    return MCUVG_OK;
}

int mcuvg_shadow_end(mcuvg_ctx *c)
{
    if (!c->depth || c->fr[c->depth - 1].pass != 1) return MCUVG_E_ARG;
    struct mcuvg_frame *f = &c->fr[c->depth - 1];
    restore(c, f);
    int rc = MCUVG_OK;
    if (f->layer) {
        float sc = mat_scale(&c->m);
        int lst = f->lst;
        for (uint32_t s = 0; s < f->g.nshadow && rc == MCUVG_OK; s++) {
            const mcuvg_shadow *sh = &f->g.shadow[s];
            float sig = sh->sigma * sc;
            float odx = c->m.a * sh->dx + c->m.c * sh->dy, ody = c->m.b * sh->dx + c->m.d * sh->dy;
            int ox = (int)(odx + (odx >= 0 ? 0.5f : -0.5f)), oy = (int)(ody + (ody >= 0 ? 0.5f : -0.5f));
            int g = iceil(3.0f * sig) + 1;
            /* the shadow's box: the group's, moved and grown by the blur's reach, inside the target */
            int sx0 = f->lx + ox - g, sy0 = f->ly + oy - g, sx1 = f->lx + ox + f->lw + g, sy1 = f->ly + oy + f->lh + g;
            if (sx0 < c->ox + c->cx0) sx0 = c->ox + c->cx0;
            if (sy0 < c->oy + c->cy0) sy0 = c->oy + c->cy0;
            if (sx1 > c->ox + c->cx1) sx1 = c->ox + c->cx1;
            if (sy1 > c->oy + c->cy1) sy1 = c->oy + c->cy1;
            if (sx0 >= sx1 || sy0 >= sy1) continue;
            /* blurred over the moved alpha grown by the reach, inside the target grown by it: all the alpha that
               reaches the laid box, and no more (the alpha itself never lies outside the target) */
            int bx0 = f->lx + ox - g, by0 = f->ly + oy - g, bx1 = f->lx + ox + f->lw + g, by1 = f->ly + oy + f->lh + g;
            if (bx0 < sx0 - g) bx0 = sx0 - g;
            if (by0 < sy0 - g) by0 = sy0 - g;
            if (bx1 > sx1 + g) bx1 = sx1 + g;
            if (by1 > sy1 + g) by1 = sy1 + g;
            int bw = bx1 - bx0, bh = by1 - by0;
            size_t mark = c->lused;
            int bs = (bw + 3) & ~3, tn = (bw > bh ? bw : bh) + 4;
            uint8_t *a = lalloc(c, (size_t)bs * (size_t)bh);
            uint32_t *t0 = (uint32_t *)(void *)lalloc(c, (size_t)tn * 4u), *t1 = (uint32_t *)(void *)lalloc(c, (size_t)tn * 4u);
            if (!a || !t0 || !t1) { rc = MCUVG_E_FULL; c->lused = mark; break; }
            for (int y = 0; y < f->lh; y++) {               /* the alpha, moved by the offset */
                int Y = f->ly + oy + y - by0;
                if (Y < 0 || Y >= bh) continue;
                for (int x = 0; x < f->lw; x++) {
                    int X = f->lx + ox + x - bx0;
                    if (X >= 0 && X < bw) a[Y * bs + X] = f->layer[y * lst + x];
                }
            }
            blur(a, bw, bh, bs, 1, sig, t0, t1);
            uint32_t col = premul(sh->color);
            for (int Y = sy0; Y < sy1; Y++) {
                int n = sx1 - sx0;
                for (int i = 0; i < n; i++) {
                    uint32_t v = a[(Y - by0) * bs + (sx0 + i - bx0)];
                    if (c->mask && v) v = (v * mask_at(c, sx0 + i, Y) + 127u) / 255u;
                    c->pix[i] = v ? scale_px(col, v + (v >> 7)) : 0u;     /* v 0..255 -> 0..256 */
                }
                blend_row(c, sx0 - c->ox, Y - c->oy, n, c->pix);
            }
            c->lused = mark;
        }
    }
    c->lused = f->lused;
    c->depth--;
    return rc;
}

/* ---- the group ------------------------------------------------------------------------------------------------------ */

int mcuvg_group_begin(mcuvg_ctx *c, const mcuvg_group *g)
{
    if (!g) return MCUVG_E_ARG;
    struct mcuvg_frame *f = push(c, 2, g);
    if (!f) return MCUVG_E_FULL;
    if (!(g->opacity < 0.999f) && !(g->blur > 0)) return MCUVG_OK;     /* a clip alone: drawn straight, through it */
    float sig = g->blur > 0 ? g->blur * mat_scale(&c->m) : 0.0f;
    struct box b = dev_box(c, g->x0, g->y0, g->x1, g->y1, sig > 0 ? 3.0f * sig + 1.0f : 0.0f);
    if (b.w <= 0 || b.h <= 0 || !(g->opacity > 0.001f)) { draw_nothing(c); return MCUVG_OK; }
    uint8_t *buf = lalloc(c, (size_t)b.w * (size_t)b.h * 4u);
    if (!buf) { c->depth--; restore(c, f); return MCUVG_E_FULL; }
    f->layer = buf; f->lx = b.x; f->ly = b.y; f->lw = b.w; f->lh = b.h;
    c->mask = 0;                                   /* the clip in force applies to the layer as a whole, at its end */
    retarget(c, buf, &b, MCUVG_RGBA8888, 4);
    return MCUVG_OK;
}

int mcuvg_clip_path(mcuvg_ctx *c, int rule)
{
    if (!c->depth || c->fr[c->depth - 1].pass == 0) return MCUVG_E_ARG;
    if (c->full) return MCUVG_E_FULL;
    if (!c->npt) { draw_nothing(c); return MCUVG_OK; }
    float a = 1e30f, b = -1e30f, e = 1e30f, f = -1e30f;
    for (uint32_t i = 0; i < c->npt; i++) {
        float x = c->pt[2 * i], y = c->pt[2 * i + 1];
        if (x == MARK) continue;
        a = fmin_(a, x); b = fmax_(b, x); e = fmin_(e, y); f = fmax_(f, y);
    }
    int x0 = ifloor(a), x1 = iceil(b) + 1, y0 = ifloor(e), y1 = iceil(f) + 1;
    if (x0 < c->cx0) x0 = c->cx0;
    if (y0 < c->cy0) y0 = c->cy0;
    if (x1 > c->cx1) x1 = c->cx1;
    if (y1 > c->cy1) y1 = c->cy1;
    struct box m = { x0 + c->ox, y0 + c->oy, x1 - x0, y1 - y0 };
    if (c->mask) {                                       /* inside the clip already in force */
        int ax = m.x > c->mx0 ? m.x : c->mx0, ay = m.y > c->my0 ? m.y : c->my0;
        int bx = m.x + m.w < c->mx0 + c->mw ? m.x + m.w : c->mx0 + c->mw;
        int by = m.y + m.h < c->my0 + c->mh ? m.y + m.h : c->my0 + c->mh;
        m.x = ax; m.y = ay; m.w = bx - ax; m.h = by - ay;
    }
    if (m.w <= 0 || m.h <= 0) { draw_nothing(c); return MCUVG_OK; }
    uint8_t *buf = lalloc(c, (size_t)m.w * (size_t)m.h);
    if (!buf) return MCUVG_E_FULL;
    /* the path's coverage into the mask: its points moved to the mask's corner, drawn into it as A8, moved back */
    float dx = (float)(m.x - c->ox), dy = (float)(m.y - c->oy);
    for (uint32_t i = 0; i < c->npt; i++) if (c->pt[2 * i] != MARK) { c->pt[2 * i] -= dx; c->pt[2 * i + 1] -= dy; }
    struct mcuvg_frame save;
    save.px = c->px; save.w = c->w; save.h = c->h; save.stride = c->stride; save.fmt = c->fmt;
    save.cx0 = c->cx0; save.cy0 = c->cy0; save.cx1 = c->cx1; save.cy1 = c->cy1; save.ox = c->ox; save.oy = c->oy;
    save.span = c->span; save.user = c->user;
    save.mask = c->mask; save.mx0 = c->mx0; save.my0 = c->my0; save.mw = c->mw; save.mh = c->mh; save.m = c->m;
    c->px = buf; c->w = m.w; c->h = m.h; c->stride = m.w; c->fmt = MCUVG_A8;
    c->cx0 = 0; c->cy0 = 0; c->cx1 = m.w; c->cy1 = m.h; c->ox = m.x; c->oy = m.y; c->span = 0; c->mask = 0;
    mcuvg_paint white = { MCUVG_FLAT, 0, 0xFFFFFFFFu, 0, 0, 0, 0, { 0 }, { 0 }, 0, 0, 0, 0, 0, 0, 0 };
    int rc = mcuvg_fill(c, &white, rule);
    restore(c, &save);
    for (uint32_t i = 0; i < c->npt; i++) if (c->pt[2 * i] != MARK) { c->pt[2 * i] += dx; c->pt[2 * i + 1] += dy; }
    if (rc) return rc;
    if (c->mask)                                          /* the intersection: the new coverage times the old */
        for (int y = 0; y < m.h; y++)
            for (int x = 0; x < m.w; x++) {
                uint8_t *p = &buf[y * m.w + x];
                *p = (uint8_t)((*p * mask_at(c, m.x + x, m.y + y) + 127u) / 255u);
            }
    c->mask = buf; c->mx0 = m.x; c->my0 = m.y; c->mw = m.w; c->mh = m.h;
    return MCUVG_OK;
}

int mcuvg_group_end(mcuvg_ctx *c)
{
    if (!c->depth || c->fr[c->depth - 1].pass != 2) return MCUVG_E_ARG;
    struct mcuvg_frame *f = &c->fr[c->depth - 1];
    restore(c, f);
    if (f->layer) {
        if (f->g.blur > 0) {
            int tn = (f->lw > f->lh ? f->lw : f->lh) + 4;
            uint32_t *t0 = (uint32_t *)(void *)lalloc(c, (size_t)tn * 4u), *t1 = (uint32_t *)(void *)lalloc(c, (size_t)tn * 4u);
            if (t0 && t1) blur(f->layer, f->lw, f->lh, f->lw * 4, 4, f->g.blur * mat_scale(&c->m), t0, t1);
        }
        uint32_t k = (uint32_t)(f->g.opacity * 256.0f + 0.5f);
        if (k > 256u) k = 256u;
        int x0 = f->lx, x1 = f->lx + f->lw, y0 = f->ly, y1 = f->ly + f->lh;   /* inside the restored target */
        if (x0 < c->ox + c->cx0) x0 = c->ox + c->cx0;
        if (y0 < c->oy + c->cy0) y0 = c->oy + c->cy0;
        if (x1 > c->ox + c->cx1) x1 = c->ox + c->cx1;
        if (y1 > c->oy + c->cy1) y1 = c->oy + c->cy1;
        for (int Y = y0; Y < y1; Y++) {
            const uint8_t *s = f->layer + ((size_t)(Y - f->ly) * (size_t)f->lw + (size_t)(x0 - f->lx)) * 4u;
            for (int i = 0; i < x1 - x0; i++, s += 4) {
                uint32_t p = (uint32_t)s[3] << 24 | (uint32_t)s[0] << 16 | (uint32_t)s[1] << 8 | s[2];
                uint32_t kk = k;
                if (c->mask) kk = (k * mask_at(c, x0 + i, Y) + 127u) / 255u;
                c->pix[i] = p ? scale_px(p, kk) : 0u;
            }
            blend_row(c, x0 - c->ox, Y - c->oy, x1 - x0, c->pix);
        }
    }
    c->lused = f->lused;
    c->depth--;
    return MCUVG_OK;
}
#endif
