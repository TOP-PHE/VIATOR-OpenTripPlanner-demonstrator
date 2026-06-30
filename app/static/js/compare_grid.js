/* PR-196b — shared compare-grid JS primitives.
 *
 * Extracted out of journey.html so the network-coverage cell-detail
 * modal (`app/templates/admin/network_coverage.html`) can re-use the
 * same N-column side-by-side primitive that PR-194 shipped for the
 * journey-search comparison. One source of truth = no drift between the
 * two consumers.
 *
 * Exposes a tiny namespace on `window.CompareGrid` so the existing
 * inline helpers in each consumer template (escHTML, renderOjpReference,
 * etc.) can keep their per-page identity without name collisions. Three
 * exports today:
 *
 *   CompareGrid.escHTML(s)
 *      — defensive HTML escape used inside the grid primitives.
 *      The consumer templates have their own escHTML; this one is here
 *      so the primitive is self-contained (works even if the template's
 *      inline escHTML is missing or named differently).
 *
 *   CompareGrid.renderGrid(columns, opts?)
 *      — the core primitive. `columns` is a list of
 *        {label, pillClass, body, key?} descriptors. Returns the full
 *        <div class="compare-grid compare-grid-refs ..."> HTML with one
 *        column per descriptor. --compare-cols is set inline so the
 *        grid scales to N without per-N CSS.
 *
 *   CompareGrid.tierPill(tier, score?)
 *      — renders the alignment-tier pill (viridis palette) for the
 *        cell-detail modal. `tier` is one of the AlignmentTier values
 *        the backend emits ('agree' / 'mostly_agree' / 'partial' /
 *        'disagree' / 'no_overlap' / 'one_sided_viator' /
 *        'one_sided_oebb' / 'no_service' / 'no_data'). `score` is the
 *        optional 0..1 float that renders as "0.74" inside the pill.
 *        Returns '' when tier is null/undefined so consumers can
 *        unconditionally interpolate.
 */
(function (global) {
  'use strict';

  function escHTML(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[c];
    });
  }

  // Human-readable labels for the alignment tiers. Keep the strings
  // short — they render inside a small pill so anything longer wraps
  // and breaks the layout. The same labels are reused in the matrix
  // tooltip so a single source of truth keeps the two surfaces in sync.
  var TIER_LABELS = {
    agree:            'Agree',
    mostly_agree:     'Mostly agree',
    partial:          'Partial',
    disagree:         'Disagree',
    no_overlap:       'No overlap',
    one_sided_viator: 'VIATOR-only',
    one_sided_oebb:   'OEBB-only',
    no_service:       'No service',
    no_data:          'No data',
  };

  function tierPill(tier, score) {
    if (tier === null || tier === undefined || tier === '') return '';
    var label = TIER_LABELS[tier] || tier;
    var scoreFrag = '';
    if (score !== null && score !== undefined && !Number.isNaN(Number(score))) {
      scoreFrag = ' <span class="alignment-tier-score">' + Number(score).toFixed(2) + '</span>';
    }
    return '<span class="alignment-tier-pill" data-tier="' + escHTML(tier) + '" '
         + 'title="VIATOR vs ÖBB alignment for this cell">'
         + escHTML(label) + scoreFrag + '</span>';
  }

  function renderGrid(columns, opts) {
    // columns: [{label, pillClass, body, key?}]
    // opts: { variant?: 'refs' | 'pair', extraClass?: string }
    var safeCols = Array.isArray(columns) ? columns : [];
    if (!safeCols.length) return '';
    var variant = (opts && opts.variant) || 'refs';
    var extraClass = (opts && opts.extraClass) || '';
    var rootClass = 'compare-grid';
    if (variant === 'refs') rootClass += ' compare-grid-refs';
    if (extraClass) rootClass += ' ' + extraClass;

    var headerRow = safeCols.map(function (c) {
      var pillClass = c.pillClass ? ' ' + escHTML(c.pillClass) : '';
      return '<div class="compare-header">'
           +   '<span class="engine-pill' + pillClass + '">' + escHTML(c.label || '') + '</span>'
           + '</div>';
    }).join('');

    var bodyRow = safeCols.map(function (c) {
      // body is rendered as raw HTML — caller is responsible for
      // escaping anything user-controlled. Falls back to a styled
      // "no itineraries found" placeholder so empty columns stay
      // visible (operator needs to see the source was queried).
      var body = c.body && c.body.length
        ? c.body
        : '<div class="compare-cell empty">no itineraries found</div>';
      return '<div class="compare-cell">' + body + '</div>';
    }).join('');

    return '<div class="' + rootClass + '" '
         + 'style="--compare-cols: repeat(' + safeCols.length + ', 1fr)">'
         + headerRow + bodyRow
         + '</div>';
  }

  global.CompareGrid = {
    escHTML: escHTML,
    renderGrid: renderGrid,
    tierPill: tierPill,
    TIER_LABELS: TIER_LABELS,
  };
})(typeof window !== 'undefined' ? window : globalThis);
