/*
 * mcuVG - the decoder (mcuvg.h): reads the picture a word at a time and drives mcuvg_raster.
 * Copyright (c) 2026 Ilya Osipov. MIT licence (see LICENSE).
 * The layout is SPEC.md's (and tools/mcuvg.py's); the two decoders are checked against each other by
 * tools/mcuvg_check.py.
 */
#include "mcuvg.h"

/* ---- a reader that touches the data only by aligned words (XIP flash) ----------------------------------------------- */

struct rd {
    const uint8_t *b;
    uint32_t i, len;
    uintptr_t wa;          /* the address of the word held */
    uint32_t w;
    int err;
};

static uint32_t u8(struct rd *r)
{
    if (r->i >= r->len) { r->err = 1; return 0; }
    uintptr_t a = (uintptr_t)(r->b + r->i), wa = a & ~(uintptr_t)3u;
    if (wa != r->wa) { r->w = *(const volatile uint32_t *)wa; r->wa = wa; }
    r->i++;
    return (r->w >> ((a & 3u) * 8u)) & 0xFFu;
}

static uint32_t vu(struct rd *r)
{
    uint32_t v = 0;
    for (int s = 0; s < 35; s += 7) {
        uint32_t c = u8(r);
        v |= (c & 0x7Fu) << s;
        if (!(c & 0x80u)) return v;
    }
    r->err = 1;
    return 0;
}

static int32_t zv(struct rd *r)
{
    uint32_t b = u8(r);
    if (b < 240u) return (int32_t)b - 120;
    if (b < 255u) {
        uint32_t i = (b - 240u) << 8 | u8(r);
        return i < 1920u ? 120 + (int32_t)i : -121 - (int32_t)(i - 1920u);
    }
    uint32_t v = vu(r);
    return (v & 1u) ? -(int32_t)((v + 1u) >> 1) : (int32_t)(v >> 1);
}

/* the command bits: low bit first, from byte `start` */
struct bits { struct rd *r; uint32_t start, bit; };
static uint32_t take(struct bits *k, int n)
{
    uint32_t v = 0;
    for (int j = 0; j < n; j++, k->bit++) {
        uint32_t save = k->r->i;
        k->r->i = k->start + (k->bit >> 3);
        uint32_t byte = u8(k->r);
        k->r->i = save;
        v |= ((byte >> (k->bit & 7u)) & 1u) << j;
    }
    return v;
}

/* ---- the header ----------------------------------------------------------------------------------------------------- */

int mcuvg_head(const void *data, uint32_t len, mcuvg_info *o)
{
    struct rd r = { (const uint8_t *)data, 0, len, ~(uintptr_t)0, 0, 0 };
    if (!data || len < 7u) return MCUVG_E_ARG;
    if (u8(&r) != (uint32_t)MCUVG_MAGIC0 || u8(&r) != (uint32_t)MCUVG_MAGIC1) return MCUVG_E_ARG;
    uint32_t ver = u8(&r);                            /* every version up to its own (SPEC.md 2.1) */
    if (ver < 1u || ver > (uint32_t)MCUVG_VERSION) return MCUVG_E_ARG;
    o->ver = (uint8_t)ver;
    uint32_t fl = u8(&r);
    if (fl & 0xB0u) return MCUVG_E_ARG;               /* bits 4, 5, 7 reserved */
    o->grid = (uint8_t)(fl & 15u);
    o->alpha = (uint8_t)((fl >> 6) & 1u);
    o->w = vu(&r);
    o->h = vu(&r);
    o->ncol = vu(&r);
    o->col_off = r.i;
    o->body_off = r.i + o->ncol * (o->alpha ? 4u : 3u);
    if (r.err || !o->w || !o->h || o->ncol > 4096u || o->body_off > len) return MCUVG_E_ARG;
    return MCUVG_OK;
}

/* ---- drawing -------------------------------------------------------------------------------------------------------- */

struct dec {
    struct rd r;
    const mcuvg_info *in;
    const uint32_t *recolor;
    uint32_t nrecolor;
    float k;               /* 1 / 2^F */
    uint32_t ver;
    int32_t px, py;        /* the pen, grid units, carried across shapes (and groups) */
    uint32_t ar, ary;      /* the last arc's radii and turn, carried too */
    int32_t arot;
    int have_path;
    int depth;
    int dashed;            /* a dash record is in force (for the next shape) */
};

static uint32_t color(struct dec *d, uint32_t i)
{
    if (d->recolor && i < d->nrecolor) return d->recolor[i];
    if (i >= d->in->ncol) { d->r.err = 1; return 0; }
    struct rd t = { d->r.b, d->in->col_off + i * (d->in->alpha ? 4u : 3u), d->r.len, ~(uintptr_t)0, 0, 0 };
    uint32_t r = u8(&t), g = u8(&t), b = u8(&t), a = d->in->alpha ? u8(&t) : 255u;
    if (t.err) d->r.err = 1;
    return a << 24 | r << 16 | g << 8 | b;
}

static void stops(struct dec *d, mcuvg_paint *p)
{
    uint32_t n = u8(&d->r);
#if MCUVG_V2
    if (n & 0x40u) {                                  /* version 2: the gradient's extension byte */
        if (d->ver < 2u) { d->r.err = 1; return; }
        uint32_t x = u8(&d->r);
        if (x & 0xF0u || (x & 3u) == 3u) { d->r.err = 1; return; }
        p->spread = (uint8_t)(x & 3u);
        if (x & 4u) {
            if (p->kind != MCUVG_RADIAL) { d->r.err = 1; return; }
            p->focal = 1;
            p->fx = (float)zv(&d->r) * d->k; p->fy = (float)zv(&d->r) * d->k; p->fr = (float)vu(&d->r) * d->k;
        }
        if (x & 8u) {
            if (p->kind != MCUVG_RADIAL) { d->r.err = 1; return; }
            p->ry = (float)vu(&d->r) * d->k; p->rot = (float)zv(&d->r);
        }
        n &= ~0x40u;
    }
#endif
    if (n & 0x80u) {
        if ((n & 0x7Fu) != 2u) { d->r.err = 1; return; }
        p->n = 2;
        p->off[0] = 0.0f; p->col[0] = color(d, vu(&d->r));
        p->off[1] = 1.0f; p->col[1] = color(d, vu(&d->r));
        return;
    }
    if (n < 1u || n > MCUVG_STOPS) { d->r.err = 1; return; }
    p->n = (uint8_t)n;
    for (uint32_t i = 0; i < n; i++) {
        p->off[i] = (float)u8(&d->r) * (1.0f / 255.0f);
        p->col[i] = color(d, vu(&d->r));
    }
    if (n == 1) { p->n = 2; p->off[1] = 1.0f; p->col[1] = p->col[0]; }
}

static void paint(struct dec *d, uint32_t kind, mcuvg_paint *p)
{
    p->kind = MCUVG_FLAT;
    p->n = 0;
    p->spread = 0; p->focal = 0; p->ry = 0; p->rot = 0; p->fx = p->fy = p->fr = 0;
    if (kind == 1u) { p->color = color(d, vu(&d->r)); return; }
    if (kind == 2u) {
        int32_t x0 = zv(&d->r), y0 = zv(&d->r), dx = zv(&d->r), dy = zv(&d->r);
        p->kind = MCUVG_LINEAR;
        p->x0 = (float)x0 * d->k; p->y0 = (float)y0 * d->k;
        p->x1 = (float)(x0 + dx) * d->k; p->y1 = (float)(y0 + dy) * d->k;
        stops(d, p);
        return;
    }
    if (kind == 3u) {
        int32_t cx = zv(&d->r), cy = zv(&d->r);
        uint32_t rr = vu(&d->r);
        p->kind = MCUVG_RADIAL;
        p->x0 = (float)cx * d->k; p->y0 = (float)cy * d->k; p->x1 = (float)rr * d->k; p->y1 = 0;
        stops(d, p);
    }
}

/* a path: its command count, the 3-bit commands, then the operands; built in the context (mcuvg_begin first) */
static int path(struct dec *d, mcuvg_ctx *c)
{
    uint32_t n = vu(&d->r);
    struct bits kb = { &d->r, d->r.i, 0 };
    uint32_t total = 0;                                   /* the commands' bits, to find the operands */
    for (uint32_t i = 0; i < n && !d->r.err; i++) {
        uint32_t cm = take(&kb, 3);
        if (cm == 5u) take(&kb, 3);
        total = kb.bit;
    }
    if (d->r.err) return MCUVG_E_ARG;
    d->r.i = kb.start + ((total + 7u) >> 3);
    kb.bit = 0;
    mcuvg_begin(c);
    int32_t sx = d->px, sy = d->py;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t cm = take(&kb, 3);
        switch (cm) {
        case 0: case 6:
            d->px += zv(&d->r); d->py += zv(&d->r);
            if (cm == 6) { mcuvg_move(c, (float)d->px * d->k, (float)d->py * d->k); sx = d->px; sy = d->py; }
            else mcuvg_line(c, (float)d->px * d->k, (float)d->py * d->k);
            break;
        case 1:
            d->px += zv(&d->r);
            mcuvg_line(c, (float)d->px * d->k, (float)d->py * d->k);
            break;
        case 2:
            d->py += zv(&d->r);
            mcuvg_line(c, (float)d->px * d->k, (float)d->py * d->k);
            break;
        case 3: {
            float q[6];
            for (int j = 0; j < 6; j += 2) { d->px += zv(&d->r); d->py += zv(&d->r); q[j] = (float)d->px * d->k; q[j + 1] = (float)d->py * d->k; }
            mcuvg_cubic(c, q[0], q[1], q[2], q[3], q[4], q[5]);
            break;
        }
        case 4: {
            float q[4];
            for (int j = 0; j < 4; j += 2) { d->px += zv(&d->r); d->py += zv(&d->r); q[j] = (float)d->px * d->k; q[j + 1] = (float)d->py * d->k; }
            mcuvg_quad(c, q[0], q[1], q[2], q[3]);
            break;
        }
        case 5: {
            uint32_t f = take(&kb, 3);
            if (!(f & 4u)) {
                uint32_t v = vu(&d->r);
                d->ar = d->ary = v >> 1;
                d->arot = 0;
                if (v & 1u) { d->ary = vu(&d->r); d->arot = zv(&d->r); }
            }
            d->px += zv(&d->r); d->py += zv(&d->r);
            mcuvg_arc(c, (float)d->ar * d->k, (float)d->ary * d->k, (float)d->arot, (int)(f & 1u), (int)((f >> 1) & 1u),
                      (float)d->px * d->k, (float)d->py * d->k);
            break;
        }
        default:                                    /* 7: close */
            mcuvg_close(c);
            d->px = sx; d->py = sy;
            break;
        }
        if (d->r.err) return MCUVG_E_ARG;
    }
    return MCUVG_OK;
}

/* one shape: kind read already */
static int shape(struct dec *d, mcuvg_ctx *c, uint32_t kind)
{
    mcuvg_paint fill, stroke;
    paint(d, kind & 3u, &fill);
    paint(d, (kind >> 2) & 3u, &stroke);
    float width = 0;
    uint32_t st = 0;
    if ((kind >> 2) & 3u) {
        width = (float)vu(&d->r) * d->k;
        st = u8(&d->r);
    }
    if (d->r.err) return MCUVG_E_ARG;
    if (!(kind & 32u)) {
        int rc = path(d, c);
        if (rc) return rc;
        d->have_path = 1;
    }
    if (!d->have_path) return MCUVG_E_ARG;
    if (kind & 3u) {
        int rc = mcuvg_fill(c, &fill, (kind & 16u) ? MCUVG_EVENODD : MCUVG_NONZERO);
        if (rc) return rc;
    }
    if ((kind >> 2) & 3u) {
        int rc = mcuvg_stroke(c, &stroke, width, (int)(st & 3u), (int)((st >> 2) & 3u), 1.0f + (float)(st >> 4) * 0.5f);
        if (rc) return rc;
    }
#if MCUVG_V2
    if (d->dashed) { mcuvg_dash(c, 0, 0, 0); d->dashed = 0; }   /* a dash record holds for one shape */
#endif
    return MCUVG_OK;
}

static int body(struct dec *d, mcuvg_ctx *c, int in_group);

#if MCUVG_V2
/* a group (SPEC.md 2.2): its header, then its body drawn once - or twice when it casts shadows: the first time only
   into the shadows' alpha (the pen and the arc carried exactly as the second time will) */
static int group(struct dec *d, mcuvg_ctx *c)
{
    if (d->depth >= MCUVG_DEPTH) return MCUVG_E_ARG;
    uint32_t fl = u8(&d->r);
    if (fl & 0xE0u) return MCUVG_E_ARG;
    uint32_t blen = vu(&d->r);
    int32_t gx = zv(&d->r), gy = zv(&d->r);
    uint32_t gw = vu(&d->r), gh = vu(&d->r);
    mcuvg_group g;
    g.x0 = (float)gx * d->k; g.y0 = (float)gy * d->k; g.x1 = (float)(gx + (int32_t)gw) * d->k; g.y1 = (float)(gy + (int32_t)gh) * d->k;
    g.opacity = 1.0f; g.blur = 0.0f; g.nshadow = 0;
    if (fl & 1u) g.opacity = (float)u8(&d->r) * (1.0f / 255.0f);
    if (fl & 8u) {
        uint32_t n = u8(&d->r);
        if (n < 1u || n > MCUVG_SHADOWS) return MCUVG_E_ARG;
        g.nshadow = n;
        for (uint32_t i = 0; i < n; i++) {
            g.shadow[i].dx = (float)zv(&d->r) * d->k; g.shadow[i].dy = (float)zv(&d->r) * d->k;
            g.shadow[i].sigma = (float)vu(&d->r) * d->k; g.shadow[i].color = color(d, vu(&d->r));
        }
    }
    if (fl & 16u) g.blur = (float)vu(&d->r) * d->k;
    if (d->r.err) return MCUVG_E_ARG;
    /* the clip path's place: read now (the pen moves), built again for each pass */
    uint32_t clip_at = d->r.i;
    int32_t cpx = d->px, cpy = d->py, carot = d->arot;
    uint32_t car = d->ar, cary = d->ary;
    if (fl & 2u) { int rc = path(d, c); if (rc) return rc; }
    uint32_t start = d->r.i, end = start + blen;
    if (blen < 1u || end > d->r.len || end < start) return MCUVG_E_ARG;
    int32_t spx = d->px, spy = d->py, sarot = d->arot;
    uint32_t sar = d->ar, sary = d->ary;
    int shp = d->have_path;
    d->depth++;
    for (int pass = (g.nshadow ? 1 : 2); pass <= 2; pass++) {
        int rc = pass == 1 ? mcuvg_shadow_begin(c, &g) : mcuvg_group_begin(c, &g);
        if (rc) { d->depth--; return rc; }
        if (fl & 2u) {                               /* the clip: its path again, from its own place */
            uint32_t keep = d->r.i;
            d->r.i = clip_at; d->px = cpx; d->py = cpy; d->ar = car; d->ary = cary; d->arot = carot;
            rc = path(d, c);
            if (!rc) rc = mcuvg_clip_path(c, (fl & 4u) ? MCUVG_EVENODD : MCUVG_NONZERO);
            d->r.i = keep;
            if (rc) {                                /* the pass is open: closed before leaving, so the context is
                                                        the caller's again (its target, not a layer) */
                if (pass == 1) (void)mcuvg_shadow_end(c); else (void)mcuvg_group_end(c);
                d->depth--;
                return rc;
            }
        }
        d->r.i = start; d->px = spx; d->py = spy; d->ar = sar; d->ary = sary; d->arot = sarot; d->have_path = shp;
        rc = body(d, c, 1);
        if (!rc && d->r.i != end) rc = MCUVG_E_ARG;  /* the body is exactly its length */
        int rc2 = pass == 1 ? mcuvg_shadow_end(c) : mcuvg_group_end(c);
        if (rc || rc2) { d->depth--; return rc ? rc : rc2; }
    }
    d->depth--;
    return MCUVG_OK;
}

static int dash(struct dec *d, mcuvg_ctx *c)
{
    uint32_t n = u8(&d->r);
    if (n < 1u || n > MCUVG_DASHES) return MCUVG_E_ARG;
    float v[MCUVG_DASHES];
    for (uint32_t i = 0; i < n; i++) v[i] = (float)vu(&d->r) * d->k;
    float off = (float)zv(&d->r) * d->k;
    if (d->r.err) return MCUVG_E_ARG;
    d->dashed = 1;
    return mcuvg_dash(c, v, (int)n, off) ? MCUVG_E_ARG : MCUVG_OK;
}
#endif

/* records to the end (0) or, inside a group, to its end (0x41) */
static int body(struct dec *d, mcuvg_ctx *c, int in_group)
{
    for (;;) {
        uint32_t kind = u8(&d->r);
        if (d->r.err) return MCUVG_E_ARG;
        if (!kind) return in_group ? MCUVG_E_ARG : MCUVG_OK;
        if (kind & 0x80u) return MCUVG_E_ARG;
        if (kind & 0x40u) {                          /* version 2's records */
#if MCUVG_V2
            if (d->ver < 2u) return MCUVG_E_ARG;
            int rc;
            if (kind == 0x40u) rc = group(d, c);
            else if (kind == 0x41u) return in_group ? MCUVG_OK : MCUVG_E_ARG;
            else if (kind == 0x42u) rc = dash(d, c);
            else return MCUVG_E_ARG;
            if (rc) return rc;
            continue;
#else
            return MCUVG_E_ARG;
#endif
        }
        int rc = shape(d, c, kind);
        if (rc) return rc;
    }
}

int mcuvg_draw(mcuvg_ctx *c, const void *data, uint32_t len, const uint32_t *recolor, uint32_t nrecolor)
{
    mcuvg_info in;
    int rc = mcuvg_head(data, len, &in);
    if (rc) return rc;
    struct dec d = { { (const uint8_t *)data, in.body_off, len, ~(uintptr_t)0, 0, 0 }, &in, recolor, nrecolor,
                     1.0f / (float)(1u << in.grid), in.ver, 0, 0, 0, 0, 0, 0, 0, 0 };
#if MCUVG_V2
    mcuvg_dash(c, 0, 0, 0);
#endif
    return body(&d, c, 0);
}

int mcuvg_fit(mcuvg_ctx *c, const void *data, uint32_t len, float x, float y, float w, float h, float deg)
{
    mcuvg_info in;
    int rc = mcuvg_head(data, len, &in);
    if (rc) return rc;
    float sx = w / (float)in.w, sy = h / (float)in.h, s = sx < sy ? sx : sy;
    mcuvg_mat keep = c->m;
    mcuvg_translate(c, x + w * 0.5f, y + h * 0.5f);
    if (deg != 0.0f) mcuvg_rotate(c, deg);
    mcuvg_scale(c, s, s);
    mcuvg_translate(c, -(float)in.w * 0.5f, -(float)in.h * 0.5f);
    rc = mcuvg_draw(c, data, len, 0, 0);
    c->m = keep;
    return rc;
}
