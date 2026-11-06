// mcuVG - the reference picture of an SVG: resvg renders it at the size the device will draw it, so the C rasteriser
// is judged against a real SVG engine, not against itself.  Needs @resvg/resvg-js (npm install in tools/, or a folder
// with it in RESVG_DIR).
//   node tools/ref_render.mjs IN.svg OUT.png SIZE [IN2.svg OUT2.png ...]   (SIZE: the width in px)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(process.env.RESVG_DIR || here, 'package.json'));
const { Resvg } = require('@resvg/resvg-js');

const a = process.argv.slice(2);
if (a.length < 3) {
  console.error('usage: ref_render.mjs IN.svg OUT.png SIZE [IN2.svg OUT2.png ...]');
  process.exit(2);
}
const size = Number(a[2]);
const pairs = [[a[0], a[1]]];
for (let i = 3; i + 1 < a.length; i += 2) pairs.push([a[i], a[i + 1]]);
for (const [src, dst] of pairs) {
  const svg = fs.readFileSync(src, 'utf8');
  const r = new Resvg(svg, { fitTo: { mode: 'width', value: size }, background: 'rgba(0,0,0,0)',
                             // the system's fonts: <text> is converted to outlines by the converter (tools/ttf.py)
                             // from the same fonts, and the reference must draw it too
                             font: { loadSystemFonts: true, defaultFontFamily: 'Helvetica', sansSerifFamily: 'Helvetica',
                                     serifFamily: 'Times', monospaceFamily: 'Courier' } });
  fs.writeFileSync(dst, r.render().asPng());
}
