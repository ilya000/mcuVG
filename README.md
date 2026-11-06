# mcuVG — MCU Vector Graphics

A compact vector picture format and an allocation-free, anti-aliased rasteriser in plain C, made for
microcontrollers. Designers and AIs write SVG; a converter compiles it to a few dozen or hundred bytes; the device draws
it straight from flash at any size and angle.

```
Heart icon:  SVG 309 B  ->  mcuVG 68 B  ->  drawn at 24, 48, 112 px or turned, from the same bytes
```

> Status: **draft 2** (experimental). Every file carries its format version in a byte of its own; a decoder reads every
> version up to its own, an encoder writes the lowest one that holds the picture (SPEC.md 2.1). The layout may still
> change before 1.0.
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
`tools/vg_check.py`, `tools/vg_quality.py`.

### Speed and size on a Cortex-M33

Instructions to draw (QEMU mps2-an505, `-O2`, `bench/m33/run.sh`): a Phosphor icon at 112 px **230–290 k**, the test
pictures **0.27–0.8 M** at 112 px and **1–12 M** at 480 px. Code: rasteriser 16.0 KB + decoder 2.8 KB at `-O2`,
11.9 + 2.2 KB at `-Os`.

On cores whose data memory is uncached (like the BK7258's application core) loads and stores dominate, so the
rasteriser is tuned by counting them: a QEMU plugin (`bench/m33/memcost.c`) counts accesses per memory region and per
function, and `bench/m33/cost.py` turns them into cycles with measured latencies. A 112 px icon: **1.4–3.3 ms at
480 MHz** in that model.

**Bit-exact:** the M33 build draws exactly the same pixels as the host build (checksums in `bench/m33`).

## What it promises an MCU developer

- **No allocation.** You hand the context one block of memory; nothing else is ever taken. The converter prints the
  exact block a picture needs at your display size (`mcuvg_mem(width, points, edges)`), checked on 1,518 draws.
- **No libc, no libm, no `double`.** Own sin/cos/atan2; `vsqrt` on an FPU. C99, `-Wall -Wextra -Wshadow -Wconversion`
  clean.
- **Data in place.** The decoder reads the picture with aligned 32-bit loads only and never past its length: keep
  pictures in XIP flash or ROM, no copy.
- **Bounded per-row work and memory.** Coverage is computed row by row (a row of `width + 2` floats), never a
  whole-picture buffer.
- **Robust.** A truncated or damaged picture returns an error and draws nothing more; every prefix of every test
  picture is refused (`tools/mcuvg_check.py`).
- **No recursion, no global state.** One context per caller; reentrant.
- **Deterministic.** Same pixels on the MCU and on the PC.
- **Clip to a band** (`mcuvg_clip`) to draw into a stripe of a display without a full framebuffer.
- **Targets:** RGB565 (plain or byte-swapped for SPI panels), RGBA8888, A8 masks, or a span callback for anything else
  (palette, 1-bpp, your DMA).
- **Recolour at draw time:** replace the picture's colour table (themes, tinted icons) without a new file.

## Quick start

Convert:

```
python3 tools/svg2mcuvg.py --px 240 --lua icon.svg       # icon.mcuvg, its size, the memory it needs, a Lua line
python3 tools/svg2mcuvg.py --quality 50 *.svg              # coarser grid, simpler outlines, smaller files
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

`examples/render.c` draws a file into a PPM on the host. Copy `src/` into a firmware project: two C files, two headers.

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

**Static pictures.** A file is one still picture: there is no animation, timeline or scripting in the format. Motion
belongs to the program that draws it - the same bytes drawn every frame at another position, size, angle or with other
colours (`mcuvg_fit`, the matrix, `recolor`).

## What SVG it takes

The converter reads SVG and writes the lowest format version that holds the picture (SPEC.md 2.1). Everything it drops
or simplifies is reported by name, never drawn wrong in silence (`python3 tools/svgfront.py FILE.svg` lists them).

### Version 1 (the format today)

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

### Turned into ordinary paths by the converter (no format version needed)

| SVG | How |
|---|---|
| `text`, `tspan` | the glyph outlines of a TrueType font found by its `font-family` (`tools/ttf.py`: the system's font folders and `MCUVG_FONTS`; `serif`, `sans-serif`, `monospace` map to common fonts); `font-size`, `font-weight`, `font-style`, `text-anchor`, `letter-spacing`, `x`/`y` on a `tspan`. One glyph a character on the baseline: no kerning, ligatures or shaping (labels and titles, not running text in scripts that need shaping). Fonts with CFF outlines (most `.otf`) are reported, not used |
| `marker-start`, `marker-mid`, `marker-end` | a copy of the marker's content at each vertex of `path`, `line`, `polyline`, `polygon`: `orient` (`auto`, `auto-start-reverse`, an angle), `markerUnits`, `viewBox`, `markerWidth`/`markerHeight`, `refX`/`refY`. The marker's overflow is not clipped |

The device never sees text or markers: they arrive as shapes, at the cost of their bytes.

### Not supported (reported by the converter)

- Images (`image`), `foreignObject`, `switch`.
- Masks (`mask`), clipping (`clipPath`), filters (blur, glow, shadows), patterns - **version 2** (below) takes
  clipping, blur, shadows and glow.
- Dashes (`stroke-dasharray`): drawn as a solid line - **version 2**.
- Animation of any kind (SMIL, CSS): the format is static (above).

Simplified, and reported:

- a radial gradient on an ellipse (a non-uniform `gradientTransform` or bounding box) is drawn as a circle;
- `spreadMethod` `reflect` and `repeat` are drawn as `pad`;
- the focal point (`fx`, `fy`) of a radial gradient is ignored;
- a group's opacity is multiplied into each shape, so overlapping shapes of one group show through each other;
- a stroke under a non-uniform transform keeps one width (the transform's mean scale).

### Version 2 (in development)

What designers' SVG (Figma exports, icons with a painted look) needs next:

| Feature | Why |
|---|---|
| Clipping (`clipPath`) | in almost every SVG exported from Figma; clipping to a shape (a hexagon, a rounded card) |
| Soft edge, shadow and glow (`feGaussianBlur`, `feDropShadow` on a shape) | depth and highlights of painted-looking icons; glows in UI designs |
| Exact group opacity | a translucent group whose shapes overlap |
| Dashes (`stroke-dasharray`, `stroke-dashoffset`) | the converter could cut a dash into pieces, but in the format it is far smaller |
| Gradients in full | elliptical radial gradients, the focal point, `reflect` and `repeat` |

Clipping, group opacity and blur need a **layer**: an 8-bit mask of the shape's area kept while it is drawn - about
12.5 KB for a 112 px icon, about 225 KB for a 480 px picture - and blur needs time on top. That is why these features
form their own version: a device short of memory reads version 1 and refuses a version 2 picture cleanly, while the
converter writes version 1 for every picture that needs none of them.

## Repository

| Path | What |
|---|---|
| `src/` | the library: `mcuvg.{h,c}` (decoder), `mcuvg_raster.{h,c}` (rasteriser) |
| `tools/svg2mcuvg.py`, `tools/mcuvg_gui.py` | the converter, command line and window |
| `tools/mcuvg.py`, `tools/svgfront.py` | encoder and reference decoder, SVG front end |
| `tools/ttf.py` | TrueType glyph outlines for `<text>` (no dependency) |
| `tools/vg_check.py`, `tools/mcuvg_check.py`, `tools/vg_sizes.py`, `tools/vg_quality.py` | checks against resvg, decoder vs reference, sizes, quality levels |
| `bench/m33/` | Cortex-M33 bench in QEMU, the memory-cost plugin and model, a profiler |
| `tests/svg/` | test pictures |

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
