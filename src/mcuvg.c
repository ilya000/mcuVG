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
    if (!data || len < 6u) return MCUVG_E_ARG;
    if (u8(&r) != (uint32_t)MCUVG_MAGIC0 || u8(&r) != (uint32_t)MCUVG_MAGIC1) return MCUVG_E_ARG;
    uint32_t fl = u8(&r);
    if (((fl >> 4) & 3u) != 1u || (fl & 0x80u)) return MCUVG_E_ARG;
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
    if (n & 0x80u) {
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

int mcuvg_draw(mcuvg_ctx *c, const void *data, uint32_t len, const uint32_t *recolor, uint32_t nrecolor)
{
    mcuvg_info in;
    int rc = mcuvg_head(data, len, &in);
    if (rc) return rc;
    struct dec d = { { (const uint8_t *)data, in.body_off, len, ~(uintptr_t)0, 0, 0 }, &in, recolor, nrecolor,
                     1.0f / (float)(1u << in.grid) };
    int32_t px = 0, py = 0;                       /* the pen, grid units, carried across shapes */
    uint32_t ar = 0, ary = 0;                     /* the last arc's radii and turn, carried too */
    int32_t arot = 0;
    int have_path = 0;
    for (;;) {
        uint32_t kind = u8(&d.r);
        if (d.r.err) return MCUVG_E_ARG;
        if (!kind) break;
        if (kind & 0xC0u) return MCUVG_E_ARG;
        mcuvg_paint fill, stroke;
        paint(&d, kind & 3u, &fill);
        paint(&d, (kind >> 2) & 3u, &stroke);
        float width = 0;
        uint32_t st = 0;
        if ((kind >> 2) & 3u) {
            width = (float)vu(&d.r) * d.k;
            st = u8(&d.r);
        }
        if (d.r.err) return MCUVG_E_ARG;
        if (!(kind & 32u)) {
            uint32_t n = vu(&d.r);
            struct bits kb = { &d.r, d.r.i, 0 };
            uint32_t total = 0;                           /* the commands' bits, to find the operands */
            for (uint32_t i = 0; i < n && !d.r.err; i++) {
                uint32_t cm = take(&kb, 3);
                if (cm == 5u) take(&kb, 3);
                total = kb.bit;
            }
            if (d.r.err) return MCUVG_E_ARG;
            d.r.i = kb.start + ((total + 7u) >> 3);
            kb.bit = 0;
            mcuvg_begin(c);
            int32_t sx = px, sy = py;
            for (uint32_t i = 0; i < n; i++) {
                uint32_t cm = take(&kb, 3);
                switch (cm) {
                case 0: case 6:
                    px += zv(&d.r); py += zv(&d.r);
                    if (cm == 6) { mcuvg_move(c, (float)px * d.k, (float)py * d.k); sx = px; sy = py; }
                    else mcuvg_line(c, (float)px * d.k, (float)py * d.k);
                    break;
                case 1:
                    px += zv(&d.r);
                    mcuvg_line(c, (float)px * d.k, (float)py * d.k);
                    break;
                case 2:
                    py += zv(&d.r);
                    mcuvg_line(c, (float)px * d.k, (float)py * d.k);
                    break;
                case 3: {
                    float q[6];
                    for (int j = 0; j < 6; j += 2) { px += zv(&d.r); py += zv(&d.r); q[j] = (float)px * d.k; q[j + 1] = (float)py * d.k; }
                    mcuvg_cubic(c, q[0], q[1], q[2], q[3], q[4], q[5]);
                    break;
                }
                case 4: {
                    float q[4];
                    for (int j = 0; j < 4; j += 2) { px += zv(&d.r); py += zv(&d.r); q[j] = (float)px * d.k; q[j + 1] = (float)py * d.k; }
                    mcuvg_quad(c, q[0], q[1], q[2], q[3]);
                    break;
                }
                case 5: {
                    uint32_t f = take(&kb, 3);
                    if (!(f & 4u)) {
                        uint32_t v = vu(&d.r);
                        ar = ary = v >> 1;
                        arot = 0;
                        if (v & 1u) { ary = vu(&d.r); arot = zv(&d.r); }
                    }
                    px += zv(&d.r); py += zv(&d.r);
                    mcuvg_arc(c, (float)ar * d.k, (float)ary * d.k, (float)arot, (int)(f & 1u), (int)((f >> 1) & 1u),
                              (float)px * d.k, (float)py * d.k);
                    break;
                }
                default:                                    /* 7: close */
                    mcuvg_close(c);
                    px = sx; py = sy;
                    break;
                }
                if (d.r.err) return MCUVG_E_ARG;
            }
            have_path = 1;
        }
        if (!have_path) return MCUVG_E_ARG;
        if (kind & 3u) {
            rc = mcuvg_fill(c, &fill, (kind & 16u) ? MCUVG_EVENODD : MCUVG_NONZERO);
            if (rc) return rc;
        }
        if ((kind >> 2) & 3u) {
            rc = mcuvg_stroke(c, &stroke, width, (int)(st & 3u), (int)((st >> 2) & 3u), 1.0f + (float)(st >> 4) * 0.5f);
            if (rc) return rc;
        }
    }
    return MCUVG_OK;
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
