/* Markdown -> .docx converter for the VIATOR documentation set.
 * Handles: ATX headings, pipe tables, fenced code, bullet/ordered lists,
 * blockquotes, horizontal rules, and inline **bold** / *italic* / `code` / [links].
 *
 * Usage: node md2docx.js <input.md> <output.docx> "<Title>" "<Subtitle>"
 */
const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, BorderStyle, ShadingType,
  TableOfContents, PageBreak, ExternalHyperlink, LevelFormat, convertInchesToTwip,
  ImageRun, Footer, PageNumber, Tab, TabStopType,
} = require('docx');

const [, , IN, OUT, TITLE, SUBTITLE] = process.argv;

const YEAR = new Date().getFullYear();
const OWNER = 'TrackOnPath SAS';

const CONTENT_W = 9020;           // A4 minus default 1" margins, in DXA
const MAX_IMG_W = 601;            // CONTENT_W in px at 96dpi
const MAX_IMG_H = 780;            // leaves room for the caption on an A4 page

/* Brand assets, resolved relative to the markdown file so both documents find them.
 * Two variants: the full stacked lockup for the title page, and the symbol on its
 * own for the footer — the wordmark is illegible below about 90px wide. */
const LOGO_PATH = path.resolve(path.dirname(IN), 'brand', 'trackonpath-logo.png');
const MARK_PATH = path.resolve(path.dirname(IN), 'brand', 'trackonpath-mark.png');
const LOGO = fs.existsSync(LOGO_PATH) ? fs.readFileSync(LOGO_PATH) : null;
const MARK = fs.existsSync(MARK_PATH) ? fs.readFileSync(MARK_PATH) : null;
if (!LOGO) console.warn(`  ! logo not found at ${LOGO_PATH} — title page will omit it`);
if (!MARK) console.warn(`  ! mark not found at ${MARK_PATH} — footer will omit it`);

/* PNG intrinsic size straight from the IHDR chunk — avoids an async probe. */
function pngSize(buf) {
  if (buf.length < 24 || buf.readUInt32BE(12) !== 0x49484452) return null;
  return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
}
const MONO = 'Consolas';
const CODE_FILL = 'F4F5F7';
const HEAD_FILL = 'E8EDF3';
const RULE = { style: BorderStyle.SINGLE, size: 4, color: 'C7CDD4' };
const CELL_BORDERS = { top: RULE, bottom: RULE, left: RULE, right: RULE };

const isContinuation = (l) => !!l && !!l.trim()
  && !/^\s*```/.test(l) && !/^#{1,6}\s/.test(l)
  && !/^\s*[-*+]\s/.test(l) && !/^\s*\d+\.\s/.test(l)
  && !/^\s*>/.test(l) && !/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)
  && !/^\s*!\[/.test(l)
  && !l.includes('|');

/* ---------- inline formatting ----------
 * Single recursive tokenizer. Alternation order is load-bearing:
 *   code first  — so a lone '*' inside `COVERAGE_*` can't start an italic run
 *   bold before italic — so '**' is never consumed as two '*'
 * Bold/italic recurse, which is what lets **`code` in bold** work; splitting on
 * code spans up-front (the earlier approach) orphaned the '**' pair and left
 * 208 literal asterisks in the output.
 */
const TOKEN = /(`[^`]*`)|(\*\*[\s\S]+?\*\*)|(\[[^\]]+\]\([^)]+\))|(\*(?!\s)[^*]+?(?<!\s)\*)/g;

function inline(text, base = {}) {
  const runs = [];
  const re = new RegExp(TOKEN.source, 'g');
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) runs.push(new TextRun({ ...base, text: text.slice(last, m.index) }));
    const tok = m[0];
    if (m[1]) {
      runs.push(new TextRun({ ...base, text: tok.slice(1, -1), font: MONO, size: 18, shading: { type: ShadingType.CLEAR, fill: CODE_FILL } }));
    } else if (m[2]) {
      runs.push(...inline(tok.slice(2, -2), { ...base, bold: true }));
    } else if (m[3]) {
      const mm = tok.match(/\[([^\]]+)\]\(([^)]+)\)/);
      if (mm && /^https?:\/\//i.test(mm[2])) {
        runs.push(new ExternalHyperlink({ link: mm[2], children: inline(mm[1], { ...base, style: 'Hyperlink' }) }));
      } else if (mm) {
        runs.push(...inline(mm[1], base));
      }
    } else if (m[4]) {
      runs.push(...inline(tok.slice(1, -1), { ...base, italics: true }));
    }
    last = re.lastIndex;
  }
  if (last < text.length) runs.push(new TextRun({ ...base, text: text.slice(last) }));
  return runs.length ? runs : [new TextRun({ ...base, text: '' })];
}

/* ---------- table ---------- */
const splitRow = (l) => l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());

function buildTable(rows) {
  const header = splitRow(rows[0]);
  const body = rows.slice(2).map(splitRow);
  const n = header.length;

  // weight columns by the longest cell they contain, clamped so no column collapses
  const weights = Array.from({ length: n }, (_, i) => {
    let max = header[i] ? header[i].length : 1;
    for (const r of body) if (r[i] && r[i].length > max) max = r[i].length;
    return Math.min(Math.max(max, 6), 80);
  });
  const total = weights.reduce((a, b) => a + b, 0);
  const widths = weights.map((w) => Math.max(700, Math.round((w / total) * CONTENT_W)));
  const drift = CONTENT_W - widths.reduce((a, b) => a + b, 0);
  widths[widths.length - 1] += drift;   // force exact sum

  const mkCell = (txt, i, isHead) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    borders: CELL_BORDERS,
    shading: isHead ? { type: ShadingType.CLEAR, fill: HEAD_FILL } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({
      spacing: { before: 20, after: 20 },
      children: inline(txt || '', isHead ? { bold: true, size: 19 } : { size: 19 }),
    })],
  });

  return new Table({
    columnWidths: widths,
    width: { size: CONTENT_W, type: WidthType.DXA },
    rows: [
      new TableRow({ tableHeader: true, children: header.map((h, i) => mkCell(h, i, true)) }),
      ...body.map((r) => new TableRow({ children: Array.from({ length: n }, (_, i) => mkCell(r[i], i, false)) })),
    ],
  });
}

/* ---------- main parse ---------- */
function parse(md) {
  const lines = md.split(/\r?\n/);
  const out = [];
  let i = 0;
  let figureNo = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // fenced code
    if (/^\s*```/.test(line)) {
      const lang = line.trim().replace(/^```/, '').trim();
      i++;
      const buf = [];
      while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      if (lang === 'mermaid') {
        out.push(new Paragraph({ spacing: { before: 120, after: 40 }, children: [new TextRun({ text: 'Entity-relationship diagram (Mermaid source):', italics: true, size: 18, color: '596673' })] }));
      }
      buf.forEach((cl, idx) => out.push(new Paragraph({
        spacing: { before: idx === 0 ? 60 : 0, after: idx === buf.length - 1 ? 120 : 0 },
        shading: { type: ShadingType.CLEAR, fill: CODE_FILL },
        indent: { left: 220 },
        children: [new TextRun({ text: cl || ' ', font: MONO, size: 17 })],
      })));
      continue;
    }

    // table
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]*-[-\s:|]*\|/.test(lines[i + 1])) {
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(lines[i]); i++; }
      if (rows.length >= 2) {
        out.push(buildTable(rows));
        out.push(new Paragraph({ spacing: { after: 140 }, children: [] }));
        continue;
      }
    }

    // block image: a line that is nothing but ![alt](path).
    // The markdown references .svg (so GitHub/VS Code render it inline); Word
    // cannot embed SVG reliably, so we swap in the sibling .png built by
    // build-diagrams.js and add a numbered caption from the alt text.
    const img = line.trim().match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/);
    if (img) {
      // the brand mark is already on the generated title page — skip it in the body
      if (/(^|\/)brand\//.test(img[2])) { i++; continue; }
      const png = path.resolve(path.dirname(IN), img[2].replace(/\.svg$/i, '.png'));
      if (fs.existsSync(png)) {
        const data = fs.readFileSync(png);
        const nat = pngSize(data);
        let w = MAX_IMG_W, h = nat ? Math.round((nat.h / nat.w) * MAX_IMG_W) : 320;
        if (h > MAX_IMG_H) { w = Math.round(w * (MAX_IMG_H / h)); h = MAX_IMG_H; }
        figureNo++;
        out.push(new Paragraph({
          alignment: AlignmentType.CENTER, spacing: { before: 200, after: 60 },
          children: [new ImageRun({ type: 'png', data, transformation: { width: w, height: h } })],
        }));
        out.push(new Paragraph({
          alignment: AlignmentType.CENTER, spacing: { after: 240 },
          children: [
            new TextRun({ text: `Figure ${figureNo}. `, bold: true, size: 18, color: '55606D' }),
            ...inline(img[1], { size: 18, color: '55606D', italics: true }),
          ],
        }));
      } else {
        out.push(new Paragraph({ spacing: { before: 100, after: 100 }, alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: `[missing diagram: ${img[2]}]`, italics: true, color: 'A03030', size: 18 })] }));
      }
      i++; continue;
    }

    // horizontal rule
    if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(line)) {
      out.push(new Paragraph({ spacing: { before: 100, after: 100 }, border: { bottom: RULE }, children: [] }));
      i++; continue;
    }

    // headings
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const depth = h[1].length, txt = h[2].replace(/\s*#+\s*$/, '');
      const map = { 1: HeadingLevel.HEADING_1, 2: HeadingLevel.HEADING_1, 3: HeadingLevel.HEADING_2, 4: HeadingLevel.HEADING_3, 5: HeadingLevel.HEADING_4, 6: HeadingLevel.HEADING_5 };
      if (depth === 1) { i++; continue; }   // doc title already on the title page
      out.push(new Paragraph({
        heading: map[depth],
        spacing: { before: depth === 2 ? 320 : 220, after: 110 },
        pageBreakBefore: depth === 2,
        children: inline(txt),
      }));
      i++; continue;
    }

    // blockquote
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
      out.push(new Paragraph({
        spacing: { before: 100, after: 120 },
        indent: { left: 340 },
        border: { left: { style: BorderStyle.SINGLE, size: 12, color: '8FA6BF', space: 10 } },
        children: inline(buf.join(' '), { italics: true }),
      }));
      continue;
    }

    // bullet list — with lazy continuation (a wrapped list item is ONE item,
    // otherwise bold spanning the wrap is orphaned and the tail becomes a
    // stray paragraph)
    const b = line.match(/^(\s*)[-*+]\s+(.*)$/);
    if (b) {
      const lvl = Math.min(Math.floor(b[1].length / 2), 2);
      i++;
      const parts = [b[2]];
      while (i < lines.length && isContinuation(lines[i])) { parts.push(lines[i].trim()); i++; }
      out.push(new Paragraph({ bullet: { level: lvl }, spacing: { before: 30, after: 30 }, children: inline(parts.join(' ')) }));
      continue;
    }

    // ordered list — same continuation rule
    const o = line.match(/^(\s*)\d+\.\s+(.*)$/);
    if (o) {
      const lvl = Math.min(Math.floor(o[1].length / 3), 2);
      i++;
      const parts = [o[2]];
      while (i < lines.length && isContinuation(lines[i])) { parts.push(lines[i].trim()); i++; }
      out.push(new Paragraph({ numbering: { reference: 'ordered', level: lvl }, spacing: { before: 30, after: 30 }, children: inline(parts.join(' ')) }));
      continue;
    }

    // paragraph (gather until blank / block start)
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim()
      && !/^\s*```/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])
      && !/^\s*[-*+]\s/.test(lines[i]) && !/^\s*\d+\.\s/.test(lines[i])
      && !/^\s*>/.test(lines[i]) && !/^\s*(---+)\s*$/.test(lines[i])
      && !/^\s*!\[/.test(lines[i])
      && !lines[i].includes('|')) { buf.push(lines[i]); i++; }
    out.push(new Paragraph({ spacing: { before: 60, after: 120 }, children: inline(buf.join(' ')) }));
  }
  return out;
}

/* ---------- assemble ---------- */
const md = fs.readFileSync(IN, 'utf8');
const body = parse(md);

/* body text for the licence page — kept as data so both documents stay identical */
const legal = (t, opts = {}) => new Paragraph({
  spacing: { before: opts.before ?? 0, after: opts.after ?? 140 },
  children: inline(t, { size: 19, color: '3A444F' }),
});

const titlePage = [
  ...(LOGO ? [new Paragraph({
    spacing: { before: 1700, after: 420 }, alignment: AlignmentType.CENTER,
    children: [new ImageRun({ type: 'png', data: LOGO, transformation: { width: 232, height: 128 } })],
  })] : [new Paragraph({ spacing: { before: 2300 }, children: [] })]),

  new Paragraph({ spacing: { after: 160 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: TITLE, bold: true, size: 60, color: '1F3350' })] }),
  new Paragraph({ spacing: { after: 760 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: SUBTITLE, size: 26, color: '55606D' })] }),
  new Paragraph({ alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: 'VIATOR — rail journey-planning demonstrator', size: 22 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 },
    children: [new TextRun({ text: OWNER, size: 22, bold: true })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 620 },
    children: [new TextRun({ text: '30 August 2026', size: 20, color: '55606D', italics: true })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 200 },
    border: { top: { style: BorderStyle.SINGLE, size: 4, color: 'C7CDD4', space: 12 } },
    children: [new TextRun({ text: `© ${YEAR} ${OWNER}. All rights reserved.`, size: 18, color: '6B7683' })] }),
  new Paragraph({ children: [new PageBreak()] }),

  /* ---- copyright and licence ---- */
  new Paragraph({ spacing: { after: 220 }, heading: HeadingLevel.HEADING_1,
    children: [new TextRun('Copyright and licence')] }),
  legal(`**© ${YEAR} ${OWNER}. All rights reserved.**`),
  legal(`VIATOR is designed, developed and owned by ${OWNER}. The VIATOR name, the TrackOnPath name and the TrackOnPath logo are the property of ${OWNER}.`),
  legal('The VIATOR software is distributed under the **Apache License, Version 2.0** (the “Licence”). A copy of the Licence accompanies the source distribution and is available at https://www.apache.org/licenses/LICENSE-2.0.'),
  new Paragraph({ spacing: { before: 120, after: 140 },
    children: [new TextRun({ text: 'Open-source licensing does not transfer ownership.', bold: true, size: 19, color: '1F3350' })] }),
  legal(`The Apache Licence grants recipients a broad, permissive right to use, modify and redistribute the software, together with an express grant of patent rights. It does **not** assign, sell or otherwise transfer the underlying intellectual property, which remains vested in ${OWNER}. Recipients must retain all copyright, patent, trademark and attribution notices, and must state any significant changes they make to the files.`),
  legal(`The Licence covers the software. It confers no right to use the **TrackOnPath** or **VIATOR** names, trademarks or logos, beyond the reasonable and customary use required to describe the origin of the work.`),
  legal(`This document is © ${YEAR} ${OWNER} and is distributed with the VIATOR source repository. It describes the system as at the date on the title page; chapter 12, where present, describes proposed rather than delivered work.`),
  new Paragraph({ children: [new PageBreak()] }),

  new Paragraph({ spacing: { after: 200 }, heading: HeadingLevel.HEADING_1, children: [new TextRun('Contents')] }),
  new TableOfContents('Contents', { hyperlink: true, headingStyleRange: '1-3' }),
  new Paragraph({ children: [new PageBreak()] }),
];

/* Footer: mark + attribution left, page number right. Suppressed on the title page. */
const pageFooter = new Footer({
  children: [new Paragraph({
    tabStops: [{ type: TabStopType.RIGHT, position: CONTENT_W }],
    border: { top: { style: BorderStyle.SINGLE, size: 4, color: 'D8DDE3', space: 10 } },
    spacing: { before: 40 },
    children: [
      ...(MARK ? [
        new ImageRun({ type: 'png', data: MARK, transformation: { width: 29, height: 25 } }),
        new TextRun({ text: '  ', size: 16 }),
      ] : []),
      new TextRun({ text: `© ${YEAR} ${OWNER}  ·  VIATOR  ·  Apache-2.0`, size: 16, color: '8A96A3' }),
      new TextRun({ children: [new Tab()] }),
      new TextRun({ text: 'Page ', size: 16, color: '8A96A3' }),
      new TextRun({ children: [PageNumber.CURRENT], size: 16, color: '8A96A3' }),
    ],
  })],
});
const blankFooter = new Footer({ children: [new Paragraph({ children: [] })] });

const doc = new Document({
  creator: OWNER,
  title: TITLE,
  description: SUBTITLE,
  subject: `VIATOR — © ${YEAR} ${OWNER}. Software licensed Apache-2.0.`,
  company: OWNER,
  numbering: {
    config: [{
      reference: 'ordered',
      levels: [0, 1, 2].map((l) => ({
        level: l,
        format: LevelFormat.DECIMAL,
        text: `%${l + 1}.`,
        alignment: AlignmentType.START,
        style: { paragraph: { indent: { left: convertInchesToTwip(0.3 * (l + 1)), hanging: 260 } } },
      })),
    }],
  },
  styles: {
    default: { document: { run: { font: 'Calibri', size: 21 }, paragraph: { spacing: { line: 276 } } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 34, bold: true, color: '1F3350' } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 27, bold: true, color: '2E4A6B' } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 23, bold: true, color: '3D5A78' } },
      { id: 'Heading4', name: 'Heading 4', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 21, bold: true, italics: true, color: '4A6580' } },
    ],
  },
  sections: [{
    properties: {
      titlePage: true,                       // lets the cover carry no footer
      page: { margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 } },
    },
    footers: { default: pageFooter, first: blankFooter },
    children: [...titlePage, ...body],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log(`wrote ${OUT} (${(buf.length / 1024).toFixed(1)} KB, ${body.length} blocks)`);
});
