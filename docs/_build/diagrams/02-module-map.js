/* The module map — what the codebase is made of and which way the arrows point.
 * Facts from facts-modules.md: 234 internal import statements across 89 files,
 * parsed with ast (function-scope lazy imports included).
 * Only the load-bearing edges are drawn; drawing all 234 would defeat the purpose.
 */
const { Svg } = require('../svgkit');

exports.id = 'arch-module-map';
exports.caption = 'Module map. Five layers, two processes, dependencies pointing downward — with the one back-edge marked.';

exports.svg = () => {
  const s = new Svg(800, 596);

  const BAND = { fill: '#FAFBFC', solid: true, color: '#EDEFF2' };
  const bands = [
    { y: 56,  h: 58, n: 'L4', l: 'entry points' },
    { y: 148, h: 58, n: 'L3', l: 'features' },
    { y: 248, h: 54, n: 'L2', l: 'helpers' },
    { y: 336, h: 54, n: 'L1', l: 'cross-cutting' },
    { y: 424, h: 54, n: 'L0', l: 'leaves' },
  ];
  for (const b of bands) {
    s.frame(100, b.y - 8, 678, b.h + 16, null, BAND);
    s.text(10, b.y + b.h / 2 - 2, b.n, { size: 12, weight: 700, fill: '#1F3864' });
    s.text(10, b.y + b.h / 2 + 13, b.l, { size: 9.5, fill: '#8A96A3' });
  }

  // ---- L4: the two processes
  s.box('web',    104, 56, 290, 58, 'main.py', { kind: 'container', sub: 'web container · 14 routers' });
  s.box('worker', 480, 56, 290, 58, 'worker.py', { kind: 'container', sub: 'worker container · poll loop' });
  s.body.push('<path d="M 437 46 V 126" stroke="#A9691A" stroke-width="1.4" stroke-dasharray="4 4" fill="none"/>');
  s.text(437, 22, 'no import edge —', { size: 10, anchor: 'middle', fill: '#A9691A', weight: 600 });
  s.text(437, 36, 'coupled only by files', { size: 10, anchor: 'middle', fill: '#A9691A' });

  // ---- L3: features
  s.box('api',   104, 148, 170, 58, 'api/', { kind: 'module', sub: 'HTTP routes' });
  s.box('journey',286, 148, 140, 58, 'journey/', { kind: 'module', sub: 'engines + oracles' });
  s.box('nc',    438, 148, 170, 58, 'network_coverage/', { kind: 'module', sub: 'the matrix', size: 12 });
  s.box('orch',  620, 148, 150, 58, 'sessions_', { kind: 'module', sub: 'orchestrator', size: 12 });

  // ---- L2: domain helpers
  s.box('tmpl',  104, 248, 130, 54, 'templating', { kind: 'module', size: 12 });
  s.box('geo',   246, 248, 120, 54, 'geo_gtfs', { kind: 'module', size: 12 });
  s.box('gbh',   378, 248, 170, 54, 'graph_build_helpers', { kind: 'module', size: 11.5 });
  s.box('nap',   560, 248, 210, 54, 'nap_ingest', { kind: 'module', sub: 'detect · ingest · sweep', size: 12 });

  // ---- L1: cross-cutting
  s.box('sec',   104, 336, 160, 54, 'security + auth', { kind: 'muted', size: 12 });
  s.box('cfg',   276, 336, 130, 54, 'config', { kind: 'muted', size: 12 });
  s.box('obs',   418, 336, 150, 54, 'observability', { kind: 'muted', sub: 'main.py only', size: 12 });
  s.box('ops',   580, 336, 190, 54, 'ops_guards', { kind: 'muted', sub: 'audit · limits · retention', size: 12 });

  // ---- L0: leaves
  s.box('models',180, 424, 190, 54, 'models/', { kind: 'store', sub: 'in 14 · out 0' });
  s.box('db',    382, 424, 110, 54, 'db', { kind: 'store', size: 12 });
  s.box('set',   504, 424, 140, 54, 'settings', { kind: 'store', sub: 'in 10 · out 0', size: 12 });

  // ---- edges worth drawing
  s.arrow(['web', 'b', -60], ['api', 't'], { route: 'orth', mid: 132 });
  s.arrow(['api', 't', 60], ['nc', 't'], { route: 'orth', mid: 132 });
  s.arrow(['api', 'r'], ['journey', 'l'], {});
  s.arrow(['web', 'b', 100], ['orch', 't'], { route: 'orth', mid: 142, dashed: true, soft: true });

  // the journey <-> network_coverage pair: one real edge each way
  s.arrow(['nc', 'l', -9], ['journey', 'r', -9], {});
  s.arrow(['journey', 'r', 9], ['nc', 'l', 9], { dashed: true, color: '#A9691A' });
  // kept left of x=500: the worker→gbh arrow turns horizontally at y=226
  s.text(330, 228, 'the only back-edge: hafas_client → external_verify', { size: 10, anchor: 'middle', fill: '#A9691A' });

  // worker's own helpers: one down the gap in L3, one round the right channel
  s.arrow(['worker', 'b', -11], ['gbh', 't', 40], { route: 'orth', mid: 226 });
  s.arrow(['worker', 'r'], ['nap', 'r'], { route: 'orth', mid: 786 });

  s.text(24, 508, 'Dependencies point downward only. models/ and settings.py import nothing at all — they are the sink of the whole graph.', { size: 10.5 });
  s.text(24, 526, 'The worker never imports api, journey or network_coverage. Coverage runs execute in the web container, not the worker.', { size: 10.5 });
  s.text(24, 544, 'app/templates and app/static hold no Python; they are assets reached through templating. About a third of api’s imports are function-scope.', { size: 10.5 });
  s.text(24, 570, 'Second exception, not shown: osm_geo ⇄ gtfs_cross_border_filter, a real cycle deliberately broken by a lazy import.', { size: 10.5, italic: true, fill: '#8A96A3' });

  return s.render();
};
