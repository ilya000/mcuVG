# mcuVG — MCU Vector Graphics

A compact vector picture format and an allocation-free, anti-aliased rasteriser in plain C, made for
microcontrollers. Designers and AIs write SVG; a converter compiles it to a few dozen or hundred bytes; the device draws
it straight from flash at any size and angle.

```
Heart icon:  SVG 309 B  ->  mcuVG 67 B  ->  drawn at 24, 48, 112 px or turned, from the same bytes
```

> Status: **draft 1** (experimental). The format carries a version; files may still change before 1.0.
> Born for the WOWCube 2.0 (Beken BK7258, Cortex-M33), meant for any MCU.

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
| Phosphor regular (1512) | 522 B | 293 B | 211 B | **126 B** |
| Phosphor fill (1512) | 470 B | 269 B | 183 B | **108 B** |
| Tests: strokes, gradients, arcs, transforms (6) | 433 B | 247 B | 111 B | **89 B** |

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
cube.vector("mcuvg:bVYSgAKAAgEAAAABAAletWqrD/NI8SB48KD4pvBrZoFAWnhmb/im9+p4+B/wA/CA9//LeMCcpbSlPMBUy3jwgPCAAA==")
```

## The format

`SPEC.md`: a 3-byte header (magic `mV`, grid, version), a colour table, then shapes. Each shape is one path filled
and/or stroked: lines, horizontal/vertical lines, quadratic and cubic Béziers, SVG elliptical arcs; non-zero or even-odd
fill; strokes with butt/round/square caps, miter/round/bevel joins and a miter limit; flat colours and linear/radial
gradients of up to 8 stops. Coordinates are deltas on a grid the converter picks from the display size: 69 % of them
fit in one byte. Commands take 3 bits. Half of the arcs in icons repeat the previous radius and do not store it.

Not in the format (the converter reports them): text, images, masks, clipping, filters, group opacity, dashes.

## Repository

| Path | What |
|---|---|
| `src/` | the library: `mcuvg.{h,c}` (decoder), `mcuvg_raster.{h,c}` (rasteriser) |
| `tools/svg2mcuvg.py`, `tools/mcuvg_gui.py` | the converter, command line and window |
| `tools/mcuvg.py`, `tools/svgfront.py` | encoder and reference decoder, SVG front end |
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
