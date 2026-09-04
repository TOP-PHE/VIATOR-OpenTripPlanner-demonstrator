/* Target architecture for chapter 12 — where the proposed API surface sits.
 * Purple = proposed, not built. Blue = exists today and is reused unchanged.
 * The load-bearing claim: the API sits ABOVE the engines, so neither MOTIS's nor
 * OTP's roadmap is on the critical path.
 */
const { Svg } = require('../svgkit');

exports.id = 'arch-ojp-target';
exports.caption = 'Proposed API surface. Purple is new; everything below the fanout already exists and is reused unchanged.';

exports.svg = () => {
  const s = new Svg(800, 618);

  s.box('ojpcli', 60, 14, 280, 50, 'OJP clients', { kind: 'actor', sub: 'LinkingAlps · EU-Spirit', size: 12.5 });
  s.box('cmpcli', 440, 14, 280, 50, 'comparison clients', { kind: 'actor', sub: 'analysts · dashboards', size: 12.5 });

  s.frame(20, 92, 760, 118, 'proposed — the VIATOR API surface', { color: '#6B2E7A' });
  s.box('ojpep',  40, 120, 230, 74, 'POST /ojp', { kind: 'future', sub: 'VIATOR trips only', badge: 'Stage 2', size: 12.5 });
  s.box('planep', 288, 120, 210, 74, 'POST /api/v1/plan', { kind: 'future', sub: 'versioned REST/JSON', badge: 'Stage 1', size: 12 });
  s.box('cmpep',  516, 120, 244, 74, 'POST /api/v1/compare', { kind: 'future', sub: 'VIATOR + oracles + scoring', badge: 'Stage 2', size: 12 });

  s.box('fanout', 170, 250, 460, 60, 'the existing fanout', { kind: 'module', sub: '/api/journey/fanout — reused unchanged' });
  s.box('dispatch', 80, 352, 280, 58, 'planner_dispatch', { kind: 'module', sub: 'the engine seam' });
  s.box('oracles',  440, 352, 280, 58, 'oracle registry', { kind: 'module', sub: 'selectable per run and country' });
  s.box('motis',    80, 448, 280, 62, 'MOTIS sessions', { kind: 'engine', sub: 'fed from National Access Points' });
  s.box('orlist',  440, 442, 280, 74,
    ['ÖBB HAFAS · Swiss OJP', 'Digitransit (FI + EE)', 'NAPCORE demonstrator'], { kind: 'external', size: 11.5 });

  s.arrow(['ojpcli', 'b'], ['ojpep', 't'], { color: '#6B2E7A' });
  s.arrow(['cmpcli', 'b'], ['cmpep', 't'], { color: '#6B2E7A' });
  s.arrow(['ojpep', 'b'], ['fanout', 't', -150], { route: 'orth', mid: 228, color: '#6B2E7A' });
  s.arrow(['planep', 'b'], ['fanout', 't'], { route: 'orth', mid: 234, color: '#6B2E7A' });
  s.arrow(['cmpep', 'b'], ['fanout', 't', 150], { route: 'orth', mid: 228, color: '#6B2E7A' });
  s.arrow(['fanout', 'b', -160], ['dispatch', 't'], { route: 'orth', mid: 332 });
  s.arrow(['fanout', 'b', 160], ['oracles', 't'], { route: 'orth', mid: 332 });
  s.arrow(['dispatch', 'b'], ['motis', 't'], {});
  s.arrow(['oracles', 'b'], ['orlist', 't'], { color: '#A9691A' });

  // kept left of x=400, where the middle descent runs
  s.text(24, 240, 'the API sits above the engines', { size: 10.5, italic: true, fill: '#6B2E7A' });

  s.text(24, 552, 'ojp_client.py is already 723 lines of OJP 2.0 XML proven against the live Swiss endpoint. Stage 2 inverts it:', { size: 10.5 });
  s.text(24, 568, 'parse an inbound OJPTripRequest, run the existing fanout, emit TripResults. Marshalling, not protocol research.', { size: 10.5 });
  s.text(24, 584, 'Because the surface sits above the engine seam, neither MOTIS’s nor OTP’s roadmap is on the critical path.', { size: 10.5 });
  s.text(24, 606, 'Oracle trips never enter the /ojp response — a standard OJP consumer would silently attribute them to VIATOR.',
    { size: 10.5, italic: true, fill: '#A9691A' });

  return s.render();
};
