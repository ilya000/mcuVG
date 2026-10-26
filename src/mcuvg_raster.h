/*
 * mcuVG - the path rasteriser: paths in, anti-aliased pixels out, for microcontrollers.
 * Copyright (c) 2026 Ilya Osipov. MIT licence (see LICENSE).
 *
 * What it does.  A path (move / line / quadratic / cubic / arc / close) is taken through the matrix, flattened in
 * DEVICE pixels (so a picture turned or scaled keeps smooth curves) and filled with exact area coverage, row by row:
 * a row accumulator of w + 2 floats, never a whole-picture buffer.  A stroke is turned into its outline here (caps
 * butt / round / square, joins miter / round / bevel) and filled the same way.  Paint: a flat colour or a linear /
 * radial gradient of up to MCUVG_STOPS stops.  Fill rules non-zero and even-odd.
 *
 * What it needs.  No libc, no malloc, no double: the caller gives one block of memory (edges, the row, the active
 * list), single-precision float only (an FPU such as the Cortex-M4F/M7/M33's), its own sin / cos / atan2.  A path that does not fit the block
 * is not drawn half: mcuvg_fill returns MCUVG_E_FULL and draws nothing.
 *
 * Targets.  RGBA8888 premultiplied, RGB565 plain or byte-swapped (as many SPI / parallel panels take it)
 * (an application's raster), A8 (a mask for tinting), or a span callback for anything else (an L8 raster through a
 * palette).  Everything is drawn "over" what the target holds.
 */
#pragma once
#include <stddef.h>
#include <stdint.h>

enum {
    MCUVG_RGBA8888 = 1,     /* 4 bytes r, g, b, a; premultiplied */
    MCUVG_RGB565 = 2,       /* uint16 plain 565, opaque */
    MCUVG_RGB565_SWAP = 3,  /* uint16 565 with its bytes swapped (big-endian panels) */
    MCUVG_A8 = 4,           /* uint8 coverage x alpha, accumulated "over" */
    MCUVG_SPAN = 5,         /* the callback below gets every span */
};
enum { MCUVG_FLAT = 0, MCUVG_LINEAR = 1, MCUVG_RADIAL = 2 };
enum { MCUVG_NONZERO = 0, MCUVG_EVENODD = 1 };
enum { MCUVG_BUTT = 0, MCUVG_ROUND = 1, MCUVG_SQUARE = 2 };              /* caps */
enum { MCUVG_MITER = 0, MCUVG_JOIN_ROUND = 1, MCUVG_BEVEL = 2 };         /* joins */
enum { MCUVG_OK = 0, MCUVG_E_FULL = -1, MCUVG_E_ARG = -2 };
#define MCUVG_STOPS 8

typedef struct { float a, b, c, d, e, f; } mcuvg_mat;                     /* x' = a x + c y + e, y' = b x + d y + f */

typedef struct {
    uint8_t kind;           /* MCUVG_FLAT / LINEAR / RADIAL */
    uint8_t n;              /* stops used (gradients: 2..MCUVG_STOPS) */
    uint32_t color;         /* flat: 0xAARRGGBB, straight alpha */
    float x0, y0, x1, y1;   /* linear: from (x0, y0) to (x1, y1); radial: centre (x0, y0), radius x1; user space */
    float off[MCUVG_STOPS]; /* stops: offsets 0..1, rising */
    uint32_t col[MCUVG_STOPS];
} mcuvg_paint;

/* a span for MCUVG_SPAN: n pixels from (x, y), each a premultiplied 0xAARRGGBB already scaled by its coverage */
typedef void (*mcuvg_span_fn)(void *user, int x, int y, int n, const uint32_t *argb);

typedef struct { float x0, y0, x1, y1; } mcuvg_edge;

typedef struct {
    /* the target */
    void *px;
    int w, h, stride;                  /* stride in bytes */
    int fmt;
    mcuvg_span_fn span;
    void *user;
    int cx0, cy0, cx1, cy1;            /* clip, inside the target */
    /* the matrix of the paths given from now on */
    mcuvg_mat m;
    float tol;                         /* flattening tolerance, device pixels (0.1) */
    /* the memory: the path's flattened points (device space; a subpath ends with a marker point), the edges a fill or
       a stroke makes of them, the row's active edges, the row's accumulator and the span's colours */
    float *pt;
    uint32_t npt, cappt;
    mcuvg_edge *e;
    uint32_t ne, cap;
    uint16_t *act;
    float *row;                        /* w + 2 */
    uint32_t *tb;                      /* the row's touched cells, a bit each */
    uint32_t *pix;                     /* w */
    /* the path being built */
    uint32_t sub;                      /* index of the current subpath's first point */
    float ux, uy, usx, usy;            /* current point and subpath start, user space (arcs are solved there) */
    int open;                          /* a subpath has begun */
    int full;                          /* the points ran out: the path is not drawn */
    uint32_t hw_pt, hw_e;              /* the most points and edges one path has taken since init */
} mcuvg_ctx;

/* the memory a context for a w-wide target needs so that one path may take `points` flattened points and `edges`
   edges (a fill takes an edge a point, a stroke's outline more: round caps and joins); the converter prints both for
   a picture at a given size.  112 px icons need a few hundred points, a 480 px picture a few thousand */
size_t mcuvg_mem(int w, uint32_t points, uint32_t edges);
/* bind the target and the memory; the matrix becomes the identity, the clip the whole target */
int mcuvg_init(mcuvg_ctx *c, void *px, int w, int h, int stride, int fmt, void *mem, size_t len);
void mcuvg_span(mcuvg_ctx *c, mcuvg_span_fn fn, void *user);
void mcuvg_clip(mcuvg_ctx *c, int x0, int y0, int x1, int y1);
void mcuvg_tolerance(mcuvg_ctx *c, float px);   /* curves flattened to within px device pixels (0.1) */

/* how much of the block the busiest path took since init: size a device's static block from these (a host tool draws
   the picture once at the size the device will use and prints them; mcuvg_mem(w, points, edges) is then enough) */
void mcuvg_usage(const mcuvg_ctx *c, uint32_t *points, uint32_t *edges);

/* the matrix: set, or multiply (the new one acts first, as in SVG's transform lists) */
void mcuvg_identity(mcuvg_ctx *c);
void mcuvg_set(mcuvg_ctx *c, const mcuvg_mat *m);
void mcuvg_concat(mcuvg_ctx *c, const mcuvg_mat *m);
void mcuvg_translate(mcuvg_ctx *c, float x, float y);
void mcuvg_scale(mcuvg_ctx *c, float sx, float sy);
void mcuvg_rotate(mcuvg_ctx *c, float deg);

/* a path in user space */
void mcuvg_begin(mcuvg_ctx *c);
void mcuvg_move(mcuvg_ctx *c, float x, float y);
void mcuvg_line(mcuvg_ctx *c, float x, float y);
void mcuvg_quad(mcuvg_ctx *c, float x1, float y1, float x, float y);
void mcuvg_cubic(mcuvg_ctx *c, float x1, float y1, float x2, float y2, float x, float y);
void mcuvg_arc(mcuvg_ctx *c, float rx, float ry, float rot_deg, int large, int sweep, float x, float y);
void mcuvg_close(mcuvg_ctx *c);

/* draw the path built since mcuvg_begin; it stays, so one path can be filled and then stroked.  The stroke's width
   is in user units (taken through the matrix's mean scale) */
int mcuvg_fill(mcuvg_ctx *c, const mcuvg_paint *p, int rule);
int mcuvg_stroke(mcuvg_ctx *c, const mcuvg_paint *p, float width, int cap, int join, float miter_limit);

/* the maths both sides share (no libm on the app side) */
float mcuvg_sin(float a);
float mcuvg_cos(float a);
float mcuvg_atan2(float y, float x);
float mcuvg_sqrt(float v);
