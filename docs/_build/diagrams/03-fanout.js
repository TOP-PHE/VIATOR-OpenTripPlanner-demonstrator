/* The journey fanout — one search, every serving session, plus the oracles.
 * Facts from facts-fanout.md (call order read out of app/api/journey.py).
 * Three things this diagram is careful about:
 *   - the oracle tasks are created BEFORE the gather, so they overlap it
 *   - the merge key is trip_signature, NOT transit_fingerprint
 *   - the gate releases before the DB writes and the merge
 */
const { Svg } = require('../svgkit');

exports.id = 'arch-journey-fanout';
exports.caption = 'One journey search, broadcast to every serving session and to the reference planners at the same time.';

exports.svg = () => {
  const s = new Svg(800, 884);

  s.box('browser', 280, 14, 240, 42, 'Operator’s browser', { kind: 'actor', sub: '/journey' });
  s.box('route',   250, 78, 300, 48, 'POST /api/journey/fanout', { kind: 'module', size: 12.5 });
  s.box('select',  230, 150, 340, 58, '_select_fanout_sessions', { kind: 'module', sub: "state='serving' AND include_in_fanout" });
  s.box('gate',    230, 230, 340, 54, 'concurrency gate', { kind: 'module', sub: 'rejects — never queues' });

  s.step(1, 262, 92); s.step(2, 242, 164); s.step(3, 242, 244);

  s.arrow(['browser', 'b'], ['route', 't'], {});
  s.arrow(['route', 'b'], ['select', 't'], {});
  s.arrow(['select', 'b'], ['gate', 't'], {});
  s.text(582, 176, '409 when no session', { size: 10.5, fill: '#A9691A' });
  s.text(582, 189, 'qualifies', { size: 10.5, fill: '#A9691A' });
  s.text(582, 256, '503 + Retry-After: 5', { size: 10.5, fill: '#A9691A' });
  s.text(582, 269, 'when full', { size: 10.5, fill: '#A9691A' });

  // ---- everything in here overlaps in time
  s.frame(20, 312, 760, 222, 'concurrent — inside the gate');
  s.box('gather', 40, 348, 280, 42, 'asyncio.gather over N sessions', { kind: 'module', size: 11.5 });
  s.box('dispatch', 40, 404, 280, 42, 'planner_dispatch.get_planner()', { kind: 'module', size: 11.5 });
  s.box('engines', 40, 460, 280, 48, 'otp-<sid>  /  motis-<sid>', { kind: 'engine', sub: 'HTTP :8080', size: 12 });

  s.box('ojpc', 350, 348, 190, 42, 'ojp_client', { kind: 'module', size: 12 });
  s.box('ojp',  350, 460, 190, 48, 'Swiss OJP 2.0', { kind: 'external', sub: '≤ 4 pages', size: 12 });
  s.box('hafc', 560, 348, 190, 42, 'hafas_client', { kind: 'module', size: 12 });
  s.box('haf',  560, 460, 190, 48, 'ÖBB HAFAS', { kind: 'external', sub: 'two-step', size: 12 });

  s.arrow(['gate', 'b', -70], ['gather', 't'], { route: 'orth', mid: 322 });
  s.arrow(['gate', 'b', 20], ['ojpc', 't'], { route: 'orth', mid: 330 });
  s.arrow(['gate', 'b', 70], ['hafc', 't'], { route: 'orth', mid: 322 });
  s.arrow(['gather', 'b'], ['dispatch', 't'], {});
  s.arrow(['dispatch', 'b'], ['engines', 't'], {});
  s.arrow(['ojpc', 'b'], ['ojp', 't'], { color: '#A9691A' });
  s.arrow(['hafc', 'b'], ['haf', 't'], { color: '#A9691A' });

  // placed right of the descending arrows, which occupy x≈180 and x≈500 at this height
  s.text(778, 550, 'gate releases below this line', { size: 10.5, italic: true, fill: '#A9691A', anchor: 'end' });

  // ---- after the gate
  s.box('record', 140, 578, 520, 48, 'recorder.record_execution', { kind: 'store', sub: 'journey_searches · executions · trips' });
  s.box('merge',  170, 652, 460, 58, 'merge across sessions by trip_signature',
    { kind: 'module', sub: 'DB-backed · not transit_fingerprint', size: 12.5, bold: true });
  s.box('flag',    60, 736, 330, 54, '_origin_flag', { kind: 'module', sub: 'ALL / SUBSET / <SID>_ONLY' });
  // "vs OJP", not "OJP only": transit_fingerprint is also the hash the coverage
  // alignment scorer and the federated planner use.
  s.box('cmp',    410, 736, 330, 54, '_build_comparison', { kind: 'module', sub: 'transit_fingerprint · vs OJP' });
  s.box('resp',   250, 818, 300, 44, 'response JSON', { kind: 'actor', size: 12.5 });

  s.step(4, 152, 592); s.step(5, 182, 666); s.step(6, 72, 750); s.step(7, 422, 750);

  s.arrow(['engines', 'b'], ['record', 't', -180], { route: 'orth', mid: 552 });
  s.arrow(['ojp', 'b'], ['record', 't', 60], { route: 'orth', mid: 544 });
  s.arrow(['haf', 'b'], ['record', 't', 180], { route: 'orth', mid: 536, soft: true });
  s.arrow(['record', 'b'], ['merge', 't'], {});
  s.arrow(['merge', 'b', -120], ['flag', 't'], { route: 'orth', mid: 722 });
  s.arrow(['merge', 'b', 120], ['cmp', 't'], { route: 'orth', mid: 722 });
  s.arrow(['flag', 'b'], ['resp', 't', -80], { route: 'orth', mid: 802 });
  s.arrow(['cmp', 'b'], ['resp', 't', 80], { route: 'orth', mid: 802 });

  s.text(24, 878, 'The DB writes, the merge and the comparison all run outside the gate. Reference-planner results reach the browser but are never persisted.',
    { size: 10.5, italic: true });

  return s.render();
};
