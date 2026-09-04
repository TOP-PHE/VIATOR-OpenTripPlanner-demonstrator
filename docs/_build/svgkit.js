/* svgkit.js — minimal declarative SVG builder for the VIATOR architecture diagrams.
 *
 * Design constraints that drove this:
 *  - rendered by librsvg (via sharp), so NO foreignObject and no CSS stylesheets:
 *    every attribute is set inline.
 *  - lands in a Word document, so the palette matches the docx theme (navy #1F3864)
 *    and fonts are ones Windows always has.
 *  - must stay legible when a 1000px-wide diagram is scaled into a 6.25" text column.
 */

const FONT = 'Segoe UI, Calibri, Arial, sans-serif';
const MONO = 'Consolas, Courier New, monospace';

// Node kinds. fill / stroke / text.
const KIND = {
  module:    { fill: '#E8EDF3', stroke: '#1F3864', text: '#1F3864' }, // VIATOR code
  container: { fill: '#FFFFFF', stroke: '#1F3864', text: '#1F3864' }, // process/container
  store:     { fill: '#E7F1EA', stroke: '#2E6B3E', text: '#1E4A2B' }, // persistent data
  external:  { fill: '#FBEFE0', stroke: '#A9691A', text: '#7A4A10' }, // third-party system
  actor:     { fill: '#EFEFF2', stroke: '#5A6673', text: '#33404D' }, // human / browser
  engine:    { fill: '#E4EEF6', stroke: '#215F86', text: '#154A6B' }, // routing engine
  future:    { fill: '#F5E9F7', stroke: '#6B2E7A', text: '#54215F' }, // proposed / not built
  state:     { fill: '#E8EDF3', stroke: '#1F3864', text: '#1F3864' },
  muted:     { fill: '#F4F5F7', stroke: '#9AA5B1', text: '#5A6673' },
};

const LINE = '#5A6673';
const LINE_SOFT = '#9AA5B1';

const esc = (s) => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/* ---- rough text width, in px, for centring and auto-sizing ---- */
function textW(s, size, mono = false) {
  const k = mono ? 0.601 : 0.512;          // measured against the two faces above
  let w = 0;
  for (const ch of String(s)) {
    if (mono) { w += k; continue; }
    if (/[ilj.,:;'`|!]/.test(ch)) w += k * 0.42;
    else if (/[ftIr()[\]{}/\\-]/.test(ch)) w += k * 0.58;
    else if (/[A-Z]/.test(ch)) w += k * 1.18;
    else if (/[mwMW]/.test(ch)) w += k * 1.42;
    else w += k;
  }
  return w * size;
}

class Svg {
  constructor(w, h, opts = {}) {
    this.w = w; this.h = h;
    this.body = [];
    this.nodes = {};
    this.title = opts.title || '';
  }

  /* --- a dashed grouping frame with a label in its top-left --- */
  frame(x, y, w, h, label, opts = {}) {
    const color = opts.color || LINE_SOFT;
    this.body.push(
      `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="8" fill="${opts.fill || 'none'}" ` +
      `stroke="${color}" stroke-width="1.25" stroke-dasharray="${opts.solid ? 'none' : '6 4'}"/>`
    );
    if (label) {
      const tw = textW(label, 11.5) + 10;
      this.body.push(
        `<rect x="${x + 12}" y="${y - 8}" width="${tw}" height="16" rx="3" fill="${opts.labelBg || '#FFFFFF'}"/>`,
        `<text x="${x + 17}" y="${y + 4}" font-family="${FONT}" font-size="11.5" font-weight="600" ` +
        `fill="${color === LINE_SOFT ? '#5A6673' : color}">${esc(label)}</text>`
      );
    }
    return this;
  }

  /* --- a node box. Registers itself under `id` for arrow anchoring. --- */
  box(id, x, y, w, h, title, opts = {}) {
    const k = KIND[opts.kind || 'module'];
    const sub = opts.sub;
    const badge = opts.badge;
    this.nodes[id] = { x, y, w, h, cx: x + w / 2, cy: y + h / 2 };

    this.body.push(
      `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${opts.rx ?? 6}" fill="${k.fill}" ` +
      `stroke="${k.stroke}" stroke-width="${opts.bold ? 2 : 1.4}"` +
      `${opts.dashed ? ' stroke-dasharray="5 3"' : ''}/>`
    );

    const lines = Array.isArray(title) ? title : [title];
    const fs = opts.size || 13;
    const subFs = 10.5;
    const blockH = lines.length * (fs + 2) + (sub ? subFs + 4 : 0);
    let ty = y + h / 2 - blockH / 2 + fs;

    for (const ln of lines) {
      this.body.push(
        `<text x="${x + w / 2}" y="${ty}" font-family="${FONT}" font-size="${fs}" font-weight="600" ` +
        `fill="${k.text}" text-anchor="middle">${esc(ln)}</text>`
      );
      ty += fs + 2;
    }
    if (sub) {
      this.body.push(
        `<text x="${x + w / 2}" y="${ty + 2}" font-family="${MONO}" font-size="${subFs}" ` +
        `fill="${k.text}" fill-opacity="0.75" text-anchor="middle">${esc(sub)}</text>`
      );
    }
    if (badge) {
      const bw = textW(badge, 9.5) + 12;
      this.body.push(
        `<rect x="${x + w - bw - 6}" y="${y - 7}" width="${bw}" height="15" rx="7.5" ` +
        `fill="${k.stroke}"/>`,
        `<text x="${x + w - bw / 2 - 6}" y="${y + 4}" font-family="${FONT}" font-size="9.5" ` +
        `font-weight="700" fill="#FFFFFF" text-anchor="middle">${esc(badge)}</text>`
      );
    }
    return this;
  }

  /* --- free text --- */
  text(x, y, s, opts = {}) {
    this.body.push(
      `<text x="${x}" y="${y}" font-family="${opts.mono ? MONO : FONT}" font-size="${opts.size || 11.5}" ` +
      `font-weight="${opts.weight || 400}" fill="${opts.fill || '#5A6673'}" ` +
      `text-anchor="${opts.anchor || 'start'}"${opts.italic ? ' font-style="italic"' : ''}>${esc(s)}</text>`
    );
    return this;
  }

  /* --- edge anchor on a node's side --- */
  _pt(id, side, off = 0) {
    const n = this.nodes[id];
    if (!n) throw new Error(`unknown node: ${id}`);
    switch (side) {
      case 'l': return [n.x, n.cy + off];
      case 'r': return [n.x + n.w, n.cy + off];
      case 't': return [n.cx + off, n.y];
      case 'b': return [n.cx + off, n.y + n.h];
      default:  return [n.cx, n.cy];
    }
  }

  /* --- arrow between two node sides.
   *     route: 'straight' | 'hv' | 'vh' | 'orth' (mid-split, picks axis from sides) --- */
  arrow(from, to, opts = {}) {
    const [fid, fside = 'r', foff = 0] = Array.isArray(from) ? from : [from, 'r', 0];
    const [tid, tside = 'l', toff = 0] = Array.isArray(to) ? to : [to, 'l', 0];
    const [x1, y1] = this._pt(fid, fside, foff);
    const [x2, y2] = this._pt(tid, tside, toff);
    const route = opts.route || 'straight';
    const color = opts.color || (opts.soft ? LINE_SOFT : LINE);
    const head = opts.soft ? 'ahs' : (opts.color ? `ah_${color.slice(1)}` : 'ah');

    let d;
    if (route === 'hv')      d = `M ${x1} ${y1} H ${x2} V ${y2}`;
    else if (route === 'vh') d = `M ${x1} ${y1} V ${y2} H ${x2}`;
    else if (route === 'orth') {
      const m = opts.mid ?? (fside === 'r' || fside === 'l' ? (x1 + x2) / 2 : (y1 + y2) / 2);
      d = (fside === 'r' || fside === 'l')
        ? `M ${x1} ${y1} H ${m} V ${y2} H ${x2}`
        : `M ${x1} ${y1} V ${m} H ${x2} V ${y2}`;
    } else d = `M ${x1} ${y1} L ${x2} ${y2}`;

    this.body.push(
      `<path d="${d}" fill="none" stroke="${color}" stroke-width="${opts.width || 1.5}" ` +
      `${opts.dashed ? 'stroke-dasharray="5 4" ' : ''}marker-end="url(#${head})"` +
      `${opts.markerStart ? ` marker-start="url(#${head}r)"` : ''}/>`
    );

    if (opts.label) {
      const lx = opts.lx ?? (x1 + x2) / 2;
      const ly = opts.ly ?? (y1 + y2) / 2 - 6;
      const lines = Array.isArray(opts.label) ? opts.label : [opts.label];
      const wMax = Math.max(...lines.map((l) => textW(l, 10.5)));
      this.body.push(
        `<rect x="${lx - wMax / 2 - 4}" y="${ly - 11 - (lines.length - 1) * 12}" ` +
        `width="${wMax + 8}" height="${lines.length * 12 + 3}" rx="2.5" fill="#FFFFFF" fill-opacity="0.95"/>`
      );
      lines.forEach((l, i) => {
        this.body.push(
          `<text x="${lx}" y="${ly - (lines.length - 1 - i) * 12}" font-family="${FONT}" ` +
          `font-size="10.5" fill="${opts.labelFill || '#44515E'}" text-anchor="middle">${esc(l)}</text>`
        );
      });
    }
    return this;
  }

  /* --- numbered step pill, for sequence diagrams --- */
  step(n, x, y, opts = {}) {
    const r = opts.r || 9.5;
    this.body.push(
      `<circle cx="${x}" cy="${y}" r="${r}" fill="${opts.fill || '#1F3864'}"/>`,
      `<text x="${x}" y="${y + 3.6}" font-family="${FONT}" font-size="${r * 1.15}" font-weight="700" ` +
      `fill="#FFFFFF" text-anchor="middle">${esc(n)}</text>`
    );
    return this;
  }

  legend(x, y, items, opts = {}) {
    const gap = opts.gap || 150;
    items.forEach((it, i) => {
      const ix = opts.vertical ? x : x + i * gap;
      const iy = opts.vertical ? y + i * 19 : y;
      const k = KIND[it.kind] || KIND.muted;
      this.body.push(
        `<rect x="${ix}" y="${iy - 9}" width="15" height="12" rx="2.5" fill="${k.fill}" ` +
        `stroke="${k.stroke}" stroke-width="1.2"/>`,
        `<text x="${ix + 21}" y="${iy + 1}" font-family="${FONT}" font-size="10.5" fill="#44515E">${esc(it.label)}</text>`
      );
    });
    return this;
  }

  render() {
    const marker = (id, color) =>
      `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
      `<path d="M 0 0 L 10 5 L 0 10 z" fill="${color}"/></marker>`;
    return [
      `<svg xmlns="http://www.w3.org/2000/svg" width="${this.w}" height="${this.h}" `,
      `viewBox="0 0 ${this.w} ${this.h}" font-family="${FONT}">`,
      `<defs>${marker('ah', LINE)}${marker('ahr', LINE)}${marker('ahs', LINE_SOFT)}`,
      `${marker('ahsr', LINE_SOFT)}${marker('ah_6B2E7A', '#6B2E7A')}${marker('ah_6B2E7Ar', '#6B2E7A')}`,
      `${marker('ah_A9691A', '#A9691A')}${marker('ah_A9691Ar', '#A9691A')}`,
      `${marker('ah_2E6B3E', '#2E6B3E')}${marker('ah_2E6B3Er', '#2E6B3E')}</defs>`,
      `<rect width="${this.w}" height="${this.h}" fill="#FFFFFF"/>`,
      ...this.body,
      `</svg>`,
    ].join('');
  }
}

module.exports = { Svg, KIND, FONT, MONO, LINE, LINE_SOFT, textW };
