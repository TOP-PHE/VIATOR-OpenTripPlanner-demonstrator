# Documentation build tooling

Regenerates the diagrams and the Word versions of `docs/architecture.md` and
`docs/user-guide.md`. Node 18+ only — no Python, no LibreOffice, no pandoc.

```bash
cd docs/_build
npm install docx sharp        # the only two dependencies
node build-diagrams.js ../diagrams
node md2docx.js ../architecture.md ../VIATOR-Architecture.docx "VIATOR Architecture" "System design and module reference for developers"
node md2docx.js ../user-guide.md   ../VIATOR-User-Guide.docx   "VIATOR User Guide"   "What it does, and how to use it"
```

## What is here

| File | Purpose |
|---|---|
| `svgkit.js` | Minimal declarative SVG builder — boxes, frames, routed arrows, legends. No dependencies |
| `diagrams/*.js` | One module per diagram. Each exports `{ id, caption, svg() }` |
| `build-diagrams.js` | Renders every diagram module to `.svg` (for the Markdown) and `.png` (for Word) |
| `md2docx.js` | Markdown → `.docx`: headings, tables, fenced code, lists, images, title page, licence page, footer |

## Things that will bite you

- **The `.docx` must stay under 1024 KB.** `.pre-commit-config.yaml` runs
  `check-added-large-files --maxkb=1024`. The diagram PNGs are the bulk of the file, which is why
  `build-diagrams.js` quantises them to a 128-colour palette — that halves them with no visible loss
  on flat-colour artwork. If you raise the render density, re-check the resulting `.docx` size.
- **The Markdown references `.svg`; Word embeds the `.png`.** `md2docx.js` swaps the extension when
  it meets a block image. Keep both outputs in `docs/diagrams/`.
- **Images under `brand/` are skipped in the body** — the logo is already on the generated title page.
- **librsvg renders the SVG, not a browser.** No `foreignObject`, no external CSS, no web fonts.
  Every attribute is set inline, and the fonts are ones Windows always has.
- **Close the documents in Word before rebuilding.** An open `.docx` is locked and the build fails
  with `EBUSY`.

## Verifying a build

There is no rendering step available in CI, so checks are structural. After a build, confirm the
`.docx` is a valid zip whose `word/document.xml` parses, and that the table count, the `<w:drawing>`
count and the figure captions match what the Markdown declares. A leftover `**` in the extracted
text means the inline parser missed something.
