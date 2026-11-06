# mcuVG — MCU Vector Graphics, format specification

**Draft 2, format version 1.** Status: experimental. The layout may still change before version 1.0. Every file carries
its format version in a byte of its own (section 2.1).

mcuVG is a compact binary format for vector pictures — icons, glyphs, UI shapes, flat illustrations — that a
microcontroller draws straight from flash. Authors write SVG; a converter on a computer compiles it; the device only
decodes and rasterises.

## 1. Conventions

- Little-endian. Bit fields are numbered from the least significant bit.
- **varuint** — an unsigned integer, 7 bits a byte, low group first; bit 7 set means another byte follows.
  At most 5 bytes.
- **zvar** — a signed integer:
  - one byte `b < 240` means `b − 120` (−120 … 119);
  - a byte `240 … 254` and one more byte `n` give `i = (b − 240) << 8 | n` and mean `120 + i` when `i < 1920`,
    else `−121 − (i − 1920)` (so 120 … 2039 and −121 … −2040);
  - `255` is followed by a zig-zag varuint (`v >> 1`, negated when `v & 1`) for anything larger.
- **Units.** A picture has a design box `width × height` in design units. Coordinates are integers on a grid of
  `1 / 2^F` design units; the encoder chooses `F` from the largest size the picture will be shown at.

## 2. Header

| Field | Size | Meaning |
|---|---|---|
| magic | 2 B | `'m' 'V'` (0x6D 0x56) |
| version | 1 B | the format version, 1 … 255; this document describes version 1 |
| flags | 1 B | bits 0–3: F; bit 6: colours carry alpha; bits 4, 5, 7: 0 |
| width | varuint | design box, units |
| height | varuint | design box, units |
| ncolors | varuint | colours in the table |
| colors | 3 or 4 B each | R, G, B (and A when bit 6), sRGB, straight alpha |

### 2.1. Versions

The version byte names the layout of everything after it. A later version only adds to the format (new commands,
paints, fields); a file that needs none of the additions stays at the version that already holds it.

1. **A decoder reads every version up to its own.** A newer decoder never drops an older version: pictures already
   made keep working.
2. **An encoder writes the lowest version that holds the picture.** A picture that uses nothing added later is
   written as the older version, so older decoders still draw it.

A decoder refuses a version newer than its own, as it refuses any other picture it cannot read (section 6).

### 2.2. Version 2 (in development)

Version 2 adds what needs an offscreen layer or more paint: clipping paths, soft edges with shadow and glow (a blur of a
shape's coverage), exact group opacity, dashed strokes, and gradients in full (elliptical radial, a focal point,
`reflect` and `repeat`). A layer costs an 8-bit mask of the affected area while it is drawn (about 12.5 KB for a 112 px
icon, about 225 KB for a 480 px picture), so a decoder for a small device may stay at version 1: it refuses version 2
pictures cleanly, and an encoder writes version 1 for every picture that uses none of these features (rule 2).
This section is filled in when version 2 is fixed.

## 3. Shapes

Shapes follow the header until a kind byte of 0. They are painted in order, each over what is already drawn.

| Field | Size | Meaning |
|---|---|---|
| kind | 1 B | bits 0–1 fill paint, bits 2–3 stroke paint (0 none, 1 flat, 2 linear, 3 radial); bit 4 even-odd fill rule (else non-zero); bit 5 the previous shape's path (no path data follows); bits 6–7: 0 |
| fill paint | | when bits 0–1 ≠ 0 |
| stroke paint | | when bits 2–3 ≠ 0 |
| stroke width | varuint | grid units; only with a stroke |
| stroke style | 1 B | bits 0–1 cap (0 butt, 1 round, 2 square); bits 2–3 join (0 miter, 1 round, 2 bevel); bits 4–7 miter limit `1 + n / 2`; only with a stroke |
| path | | unless bit 5 |

A shape is filled first, then stroked.

### 3.1 Paints

- **Flat:** varuint colour index.
- **Linear gradient:** zvar x0, zvar y0 (absolute, grid units), zvar dx, zvar dy (from the start to the end point),
  stops.
- **Radial gradient:** zvar cx, zvar cy (absolute), varuint r, stops.
- **Stops:** one byte `n`. With bit 7 set, `n & 127` must be 2 and the offsets are 0 and 1: two varuint colour
  indices follow. Otherwise `n` stops (1 … 8) of a byte offset (0 … 255 for 0 … 1) and a varuint colour index each,
  offsets not decreasing. Outside the stops the end colours extend (pad).
- Gradients are interpolated in premultiplied sRGB.

### 3.2 Paths

```
varuint ncmd
commands: 3 bits each, low bits first, packed to a whole byte; an arc's command has 3 more bits
operands: in command order
```

| Code | Command | Operands |
|---|---|---|
| 0 | line | zvar dx, dy |
| 1 | horizontal line | zvar dx |
| 2 | vertical line | zvar dy |
| 3 | cubic Bézier | three points, zvar dx, dy each, every point from the previous one |
| 4 | quadratic Bézier | two points, as above |
| 5 | elliptical arc | 3 more command bits: bit 0 large arc, bit 1 sweep (as SVG), bit 2 the previous arc's radii; unless bit 2: varuint `r << 1 \| ellipse`, and when ellipse: varuint ry, zvar rotation in degrees; then zvar dx, dy of the end point |
| 6 | move | zvar dx, dy |
| 7 | close | none |

- Every offset is from the **pen**: the last point written, carried from shape to shape (the origin at the start of the
  picture). Close moves the pen back to the subpath's start.
- The previous arc's radii are carried from shape to shape as well.
- Arcs follow SVG's endpoint parametrisation (SVG 1.1 F.6.5), including the scaling of radii that do not reach.
- A fill closes every subpath implicitly.

## 4. Rendering rules

- Coverage: the exact area of the shape inside each pixel (anti-aliasing is part of the format's look).
- Fill rules non-zero and even-odd as in SVG.
- Strokes: the outline of the polyline with the given width, caps, joins and miter limit, filled non-zero.
- Compositing: source-over, premultiplied alpha.
- Curve flattening tolerance is the renderer's choice; the reference renderer uses 0.1 device pixel and places arc
  vertices half a sagitta outside the arc, so round shapes keep their area.

## 5. Embedding in text

An mcuVG picture is often carried inside source code, JSON (MCP tool calls) or Lua. Use **base64** (RFC 4648) with the
prefix `mcuvg:`:

```lua
mcuvg.draw("mcuvg:bVYBAoACgAIBAAAAAQAJXrVqqw/zSPEgePCg+Kbwa2aBQFp4Zm/4pvfqePgf8APwgPf/y3jAnKW0pTzAVMt48IDwgAA=")
```

As a URI: `data:image/x-mcuvg;base64,…`. File extension: `.mcuvg`.

## 6. Decoder obligations

A conforming decoder:

- reads only within the given length and refuses a picture that ends early or holds an unknown command, paint,
  version or a non-zero reserved bit;
- may read the data with aligned word loads only (so it can be executed from XIP flash or ROM in place);
- draws nothing of a shape it cannot complete.

## 7. What the format leaves out (by design, for now)

- **Animation.** A picture is static: one still image, no timeline, no scripting. Motion is the drawing program's
  (the same picture drawn each frame through another matrix or colour table).
- **Text and markers.** A converter turns them into ordinary shapes (glyph outlines, copies of the marker at the
  vertices); the format and the decoder know neither.
- **Images, masks, patterns.**
- **Clipping, filters (blur, glow, shadows), exact group opacity, dashes** - version 2 (2.2). In version 1 a
  group's opacity is approximated by multiplying the alpha of each shape, and a dashed stroke is solid.

A converter reports whatever it drops or simplifies.

## 8. Open before 1.0

- A fixed-point build of the reference renderer for cores without an FPU (the format itself is integer-only).
- Whether to state in the header the memory a picture needs at its design size (today the converter prints it).
