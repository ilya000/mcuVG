/*
 * mcuVG - MCU Vector Graphics: the decoder that draws an mcuVG picture through mcuvg_raster.
 * Copyright (c) 2026 Ilya Osipov. MIT licence (see LICENSE).
 *
 * The format is specified in SPEC.md; tools/mcuvg.py holds the encoder from SVG and the reference decoder.  A picture is a colour table and a list of shapes; each shape is one path filled
 * and/or stroked.  Coordinates are deltas on a grid of 1 / 2^F design units.
 *
 * The data may lie in XIP flash or ROM, used in place: it is read a word at a time, never a
 * byte, and never past `len`.  A picture that is cut or malformed draws nothing past the fault and returns an error.
 */
#pragma once
#include "mcuvg_raster.h"

#define MCUVG_MAGIC0 'm'
#define MCUVG_MAGIC1 'V'

typedef struct {
    uint32_t w, h;          /* the design box, units */
    uint32_t ncol;          /* colours in the table */
    uint8_t grid;           /* F: coordinates are in 1 / 2^F units */
    uint8_t alpha;          /* the table carries alpha */
    uint32_t col_off;       /* where the table starts */
    uint32_t body_off;      /* where the shapes start */
} mcuvg_info;

/* the header, checked; 0 or MCUVG_E_ARG */
int mcuvg_head(const void *data, uint32_t len, mcuvg_info *o);

/* draw the picture in design units through the context's matrix.  recolor: NULL, or ncol colours 0xAARRGGBB used
   instead of the picture's own (a theme, a tint; index i replaces colour i) */
int mcuvg_draw(mcuvg_ctx *c, const void *data, uint32_t len, const uint32_t *recolor, uint32_t nrecolor);

/* draw it into the device box (x, y, w, h), aspect kept, centred, turned by deg about the box's centre */
int mcuvg_fit(mcuvg_ctx *c, const void *data, uint32_t len, float x, float y, float w, float h, float deg);
