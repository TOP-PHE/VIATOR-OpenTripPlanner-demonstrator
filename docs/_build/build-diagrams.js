/* build-diagrams.js — render every diagram module to .svg (for the markdown,
 * which GitHub and VS Code display inline) and .png (for the Word document,
 * which cannot embed SVG reliably).
 *
 * Each module in ./diagrams exports { id, caption, svg() -> string }.
 * Usage: node build-diagrams.js <outDir>
 */
const fs = require('fs');
const path = require('path');
// sharp is installed next door in docxbuild/ — resolve it from there.
const sharp = (() => {
  try { return require('sharp'); }
  catch { return require(path.join(__dirname, '..', 'docxbuild', 'node_modules', 'sharp')); }
})();

const OUT = process.argv[2] || path.join(__dirname, 'out');
const DIR = path.join(__dirname, 'diagrams');
const DENSITY = 200;   // ~2x the 96dpi nominal size, so it stays crisp when Word scales it

fs.mkdirSync(OUT, { recursive: true });

const mods = fs.readdirSync(DIR).filter((f) => f.endsWith('.js')).sort()
  .map((f) => require(path.join(DIR, f)));

(async () => {
  for (const m of mods) {
    const svg = m.svg();
    const svgPath = path.join(OUT, `${m.id}.svg`);
    const pngPath = path.join(OUT, `${m.id}.png`);
    // trailing newline keeps the repo's end-of-file-fixer hook happy across rebuilds
    fs.writeFileSync(svgPath, `${svg}\n`, 'utf8');
    // Palette quantisation: these diagrams are flat fills plus antialiased text,
    // so 128 colours is visually lossless and roughly halves the file. That
    // matters because the repo's check-added-large-files hook caps at 1024 KB
    // and the PNGs are embedded in the .docx.
    const info = await sharp(Buffer.from(svg), { density: DENSITY })
      .png({ compressionLevel: 9, palette: true, colours: 128, dither: 0 })
      .toFile(pngPath);
    console.log(
      `${m.id.padEnd(28)} svg ${String(Math.round(svg.length / 1024)).padStart(3)} KB` +
      `   png ${String(info.width).padStart(4)}x${String(info.height).padEnd(4)} ` +
      `${(fs.statSync(pngPath).size / 1024).toFixed(0).padStart(4)} KB`
    );
  }
  console.log(`\n${mods.length} diagrams -> ${OUT}`);
})().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
