# mcuVG — MCU Vector Graphics

A compact vector picture format and an allocation-free, anti-aliased rasteriser in plain C, made for
microcontrollers. Designers and AIs write SVG; a converter compiles it to a few dozen or hundred bytes; the device draws
it straight from flash at any size and angle.

```
Heart icon:  SVG 309 B  ->  mcuVG 68 B  ->  drawn at 24, 48, 112 px or turned, from the same bytes
```

> Status: **draft 3** (experimental): format versions **1** (shapes, strokes, gradients) and **2** (clipping, shadows and
> glows, blur, exact group opacity, dashes, gradients in full). Every file carries its version in a byte of its own; a
> decoder reads every version up to its own, an encoder writes the lowest one that holds the picture (SPEC.md 2.1). The
> layout may still change before 1.0.
> Built and measured on a Cortex-M33 (Beken BK7258); meant for any MCU.

## Why

| | mcuVG | SVG on the device | Bitmaps |
|---|---|---|---|
| Size of an icon | ~70–200 B | 0.3–1 KB + an XML parser | 6–25 KB at 112 px |
| Any size, rotation | yes, crisp | yes | no (scaling blurs) |
| Parser on the MCU | a byte reader, ~2–3 KB | XML + path grammar + float parsing | — |
| RAM | one block you size in advance | heap | the bitmap |

### Size, measured on 3,024 Phosphor icons (MIT) and our test set

| Set | SVG | SVG + gzip | TinyVG | **mcuVG** |
|---|---|---|---|---|
| Phosphor regular (1512) | 522 B | 293 B | 211 B | **127 B** |
| Phosphor fill (1512) | 470 B | 269 B | 183 B | **109 B** |
| Tests: strokes, gradients, arcs, transforms (6) | 433 B | 247 B | 111 B | **90 B** |

Grid for display at 240 px, step ≤ 0.25 px. Reproduce with `tools/vg_sizes.py` (`tools/tvg_enc.py` writes TinyVG for the
comparison).

### Fidelity

Every picture is compared with [resvg](https://github.com/linebender/resvg) rendering the original SVG: mean difference
**0.1 of 255** per channel, about **0.1 % of pixels** differing by more than 32 (mostly anti-aliasing at curve joints).
Version 2's test pictures (`tests/svg2`: clips, shadows, glows, blur, group opacity, dashes, gradients): mean
**0.53 of 255**, 0.28 % of pixels off by more than 32, none over the limits. `tools/vg_check.py`, `tools/vg_quality.py`.

### Speed and size on a Cortex-M33

Instructions to draw (QEMU mps2-an505, `-O2`, `bench/m33/run.sh`): the version 1 test pictures **0.27–0.86 M** at
112 px and **1.1–12.8 M** at 480 px; version 2's **0.5–3.8 M** at 112 px.

| Code (`-Os`) | rasteriser | decoder |
|---|---|---|
| versions 1 and 2 (`MCUVG_V2=1`, the default) | 20.0 KB | 3.4 KB |
| version 1 only (`-DMCUVG_V2=0`) | 13.2 KB | 2.3 KB |

On cores whose data memory is uncached (like the BK7258's application core) loads and stores dominate, so the
rasteriser is tuned by counting them: a QEMU plugin (`bench/m33/memcost.c`) counts accesses per memory region and per
function, and `bench/m33/cost.py` turns them into cycles with measured latencies. In that model, at 480 MHz, `-Os`, into
an RGB565 target:

| Picture at 112 px | ms | | Version 2 at 112 px | ms | layers |
|---|---|---|---|---|---|
| shapes, even-odd | 3.4 | | dashes | 4.0 | 0 |
| arcs and transforms | 2.1 | | gradients in full | 8.9 | 0 |
| colours with alpha | 2.6 | | group opacity | 9.5 | 30 KB |
| strokes | 4.2 | | glow | 11.4 | 18 KB |
| linear gradient | 3.5 | | two shadows | 12.7 | 8.5 KB |
| radial gradient | 5.3 | | clipping | 13.9 | 13 KB |
| | | | blur | 17.7 | 46 KB |

The build with version 2 draws version 1 pictures as fast as the build without it. Version 2's blur is three box blurs
over 32-bit words (four values at a time; its cost does not grow with the radius).

**Bit-exact:** the M33 build draws exactly the same pixels as the host build, versions 1 and 2, at `-Os` and `-O2`
(checksums in `bench/m33`).

## What it promises an MCU developer

- **No allocation.** You hand the context one block of memory; nothing else is ever taken. The converter prints the
  exact block a picture needs at your display size (`mcuvg_mem(width, points, edges)`), checked on 1,518 draws.
  Version 2's layers take a second block you give (`mcuvg_layers`, used as a stack); the converter prints what a
  picture takes of it, and with too little a group returns `MCUVG_E_FULL` and draws nothing.
- **No libc, no libm, no `double`.** Own sin/cos/atan2; `vsqrt` on an FPU. C99, `-Wall -Wextra -Wshadow -Wconversion`
  clean.
- **Data in place.** The decoder reads the picture with aligned 32-bit loads only and never past its length: keep
  pictures in XIP flash or ROM, no copy.
- **Bounded per-row work and memory.** Coverage is computed row by row (a row of `width + 2` floats), never a
  whole-picture buffer.
- **Robust.** A truncated or damaged picture returns an error and draws nothing more; every prefix of every test
  picture is refused (`tools/mcuvg_check.py`).
- **No global state.** One context per caller; reentrant. Version 2's groups nest 6 deep at most (bounded recursion).
- **Deterministic.** Same pixels on the MCU and on the PC.
- **Clip to a band** (`mcuvg_clip`) to draw into a stripe of a display without a full framebuffer.
- **Targets:** RGB565 (plain or byte-swapped for SPI panels), RGBA8888, A8 masks, or a span callback for anything else
  (palette, 1-bpp, your DMA).
- **Recolour at draw time:** replace the picture's colour table (themes, tinted icons) without a new file.
- **Version 1 only, when that is all a device needs:** build with `-DMCUVG_V2=0` - none of version 2's code, no check
  on version 1's paths, version 2 pictures refused cleanly; the converter's `--max-version 1` writes pictures for it.

## Quick start

Convert:

```
python3 tools/svg2mcuvg.py --px 240 --lua icon.svg       # icon.mcuvg, its size, the memory it needs, a Lua line
python3 tools/svg2mcuvg.py --quality 50 *.svg              # coarser grid, simpler outlines, smaller files
python3 tools/svg2mcuvg.py --max-version 1 icon.svg         # for a device built without version 2
```

or open the window: **`mcuVG converter (Mac).command`** / **`mcuVG converter (Windows).bat`** (Python 3 with tkinter;
on Windows you can also drop SVG files on `svg2mcuvg (Windows, drop files).bat`). The memory figures and previews
need a C compiler (cc, clang, gcc or MSVC's cl); the conversion itself is pure Python.

Draw (C):

```c
#include "mcuvg.h"

static uint16_t frame[240 * 240];                 /* RGB565 */
static uint8_t  work[12 * 1024];                  /* the converter tells you how much */

void show_icon(const uint8_t *pic, uint32_t len)  /* pic may live in flash */
{
    mcuvg_ctx c;
    mcuvg_init(&c, frame, 240, 240, 240 * 2, MCUVG_RGB565, work, sizeof work);
    mcuvg_fit(&c, pic, len, 64, 64, 112, 112, 0.0f);   /* box x, y, w, h; angle in degrees */
}
```

A version 2 picture (clips, shadows, blur, group opacity) also wants the layers' memory - the converter prints how much:

```c
static uint8_t layers[16 * 1024];                 /* a 112 px icon's shadow takes ~8.5 KB */
    ...
    mcuvg_layers(&c, layers, sizeof layers);      /* after mcuvg_init; without it such a group returns MCUVG_E_FULL */
```

`examples/render.c` draws a file into a PPM on the host. Copy `src/` into a firmware project: two C files, two headers
(`-DMCUVG_V2=0` for version 1 only).

### In text: Lua, JSON, MCP

A picture travels inside code as base64 with the prefix `mcuvg:`:

```lua
mcuvg.draw("mcuvg:bVYBAoACgAIBAAAAAQAJXrVqqw/zSPEgePCg+Kbwa2aBQFp4Zm/4pvfqePgf8APwgPf/y3jAnKW0pTzAVMt48IDwgAA=")
```

## The format

`SPEC.md`: a 4-byte header (magic `mV`, the format version, flags with the grid), a colour table, then shapes. Each shape is one path filled
and/or stroked: lines, horizontal/vertical lines, quadratic and cubic Béziers, SVG elliptical arcs; non-zero or even-odd
fill; strokes with butt/round/square caps, miter/round/bevel joins and a miter limit; flat colours and linear/radial
gradients of up to 8 stops. Coordinates are deltas on a grid the converter picks from the display size: 69 % of them
fit in one byte. Commands take 3 bits. Half of the arcs in icons repeat the previous radius and do not store it.
Version 2 adds three records - a group (a clip path, up to 4 shadows or glows, a blur, an opacity, and the group's box
and length, so a decoder can size its layers and draw a group twice), its end, and dashes for the next stroke - and an
extension byte on a gradient (spread, focal circle, ellipse).

**Static pictures.** A file is one still picture: there is no animation, timeline or scripting in the format. Motion
belongs to the program that draws it - the same bytes drawn every frame at another position, size, angle or with other
colours (`mcuvg_fit`, the matrix, `recolor`).

## What SVG it takes

The converter reads SVG and writes the lowest format version that holds the picture (SPEC.md 2.1). Everything it drops
or simplifies is reported by name, never drawn wrong in silence (`python3 tools/svgfront.py FILE.svg` lists them).

### Version 1

| SVG | How |
|---|---|
| Elements | `path`, `rect` (with `rx`/`ry`), `circle`, `ellipse`, `line`, `polyline`, `polygon`; groups `g`, `a`, nested `svg`, `use` and `symbol` |
| Path commands | every one: lines, `H`/`V`, quadratic and cubic Béziers (`S`/`T` too), elliptical arcs as in SVG |
| Transforms | any: `translate`, `scale`, `rotate`, `skewX`/`skewY`, `matrix`, nested. The converter applies them; an arc stays an arc while the transform keeps circles circles, otherwise it becomes cubic curves |
| Fill | non-zero and even-odd rules |
| Stroke | width; caps `butt`, `round`, `square`; joins `miter`, `round`, `bevel`; the miter limit |
| Paint | a colour with alpha (hex, `rgb()`/`rgba()`, `hsl()`/`hsla()`, the 148 CSS colour keywords, `currentColor`; anything else is reported); linear and radial gradients of up to 8 stops (`gradientUnits`, `gradientTransform`, `href` chains); outside the stops the end colours extend (`pad`) |
| Opacity | `opacity`, `fill-opacity`, `stroke-opacity`; a group's opacity approximately - multiplied into each of its shapes |
| Styles | presentation attributes, `style=""`, `<style>` with tag and class selectors, inheritance, `display`, `visibility` |

### Version 2

| SVG | How |
|---|---|
| `clip-path` | the clip's shapes (`clipPathUnits="userSpaceOnUse"`, `clip-rule`, `use` inside), nested clips intersected |
| `opacity` on a group | exact: the group drawn as one layer, then made translucent (one shape that only fills or only strokes needs no layer: its alpha) |
| `filter` | shadows and glows - `feDropShadow`, and `feGaussianBlur` / `feOffset` of `SourceAlpha` coloured by `feFlood` with `feComposite in` or by `feColorMatrix`, merged under the drawing with `feMerge` or `feBlend normal` (the drop shadow Figma exports included); a blur of the drawing (`feGaussianBlur` of `SourceGraphic`); up to 4 shadows a group |
| `stroke-dasharray`, `stroke-dashoffset` | dashes along the stroke, up to 8 lengths, caps on every dash; a zero-length dash with round caps is a dot |
| gradients | `spreadMethod` `reflect` and `repeat`; a radial gradient's ellipse (any `gradientTransform` or bounding box); its focal point `fx`, `fy` and `fr`; a linear gradient under a skew or a non-uniform scale exactly |

Groups follow SVG's order: the filter, then the clip, then the opacity.

### Turned into ordinary paths by the converter (no format version needed)

| SVG | How |
|---|---|
| `text`, `tspan` | the glyph outlines of a TrueType font found by its `font-family` (`tools/ttf.py`: the system's font folders and `MCUVG_FONTS`; `serif`, `sans-serif`, `monospace` map to common fonts); `font-size`, `font-weight`, `font-style`, `text-anchor`, `letter-spacing`, `x`/`y` on a `tspan`. One glyph a character on the baseline: no kerning, ligatures or shaping (labels and titles, not running text in scripts that need shaping). Fonts with CFF outlines (most `.otf`) are reported, not used |
| `marker-start`, `marker-mid`, `marker-end` | a copy of the marker's content at each vertex of `path`, `line`, `polyline`, `polygon`: `orient` (`auto`, `auto-start-reverse`, an angle), `markerUnits`, `viewBox`, `markerWidth`/`markerHeight`, `refX`/`refY`. The marker's overflow is not clipped |

The device never sees text or markers: they arrive as shapes, at the cost of their bytes.

### Not supported (reported by the converter)

- Images (`image`), `foreignObject`, `switch`.
- Masks (`mask`), patterns (`pattern` paints).
- Filters other than shadows, glows and blur (inner shadows, lighting, turbulence, colour matrices on the drawing,
  blend modes other than normal); `clipPathUnits` and `primitiveUnits` of `objectBoundingBox`.
- Animation of any kind (SMIL, CSS): the format is static (above).

Simplified, and reported:

- a stroke under a non-uniform transform keeps one width (the transform's mean scale);
- an uneven blur (`stdDeviation="x y"`) is drawn with their mean;
- a focal point outside its circle is moved onto the edge (SVG 1.1's rule);
- written as version 1 (`--max-version 1`): a group's opacity is multiplied into each shape, clips, shadows and blur
  are dropped, dashes are solid, gradients pad from their centre on a circle.

By the reference renderer, not the format: a shadow is offset by whole pixels, and what a group draws outside the
target casts no shadow into it.

### Why version 2 is a version of its own

Clipping, group opacity, shadows and blur need a **layer** while a group is drawn (sizes in the table above; up to
~0.8 MB for a blur over a whole 480 px picture) and blur takes time on top. A device short of memory reads version 1
and refuses a version 2 picture cleanly, and the converter writes version 1 for every picture that needs none of it.

## Repository

| Path | What |
|---|---|
| `src/` | the library: `mcuvg.{h,c}` (decoder), `mcuvg_raster.{h,c}` (rasteriser) |
| `tools/svg2mcuvg.py`, `tools/mcuvg_gui.py` | the converter, command line and window |
| `tools/mcuvg.py`, `tools/svgfront.py` | encoder and reference decoder, SVG front end |
| `tools/ttf.py` | TrueType glyph outlines for `<text>` (no dependency) |
| `tools/vg_check.py`, `tools/mcuvg_check.py`, `tools/vg_sizes.py`, `tools/vg_quality.py` | checks against resvg, decoder vs reference, sizes, quality levels |
| `bench/m33/` | Cortex-M33 bench in QEMU, the memory-cost plugin and model, a profiler |
| `tests/svg/`, `tests/svg2/` | test pictures: version 1, version 2 |

Checks: `make check` (decoder vs reference, cut and damaged pictures), `make check-resvg` (against resvg; `cd tools &&
npm install` first).

## Roadmap

- A fixed-point build for cores without an FPU (Cortex-M0/M0+/M3, RISC-V without F).
- More targets: L8 with a palette, RGB332, 1-bpp.
- Hardware paths (DMA2D fills, 2D GPUs) and an LVGL image decoder.
- Version 1.0 of the format after it ships on devices.

## Licence

MIT, see `LICENSE`. Test icons from [Phosphor](https://phosphoricons.com) (MIT) are used in the measurements but not
shipped here.
