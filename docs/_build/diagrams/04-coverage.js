/* A network coverage run, in three phases.
 * Facts from facts-coverage.md (runner.py + api/admin/network_coverage.py).
 * The two things prose keeps getting wrong and this diagram fixes:
 *   - it runs in the web container, after the HTTP response has been sent
 *   - external verification is Phase 3, after ALL routing, and a cancel skips it
 */
const { Svg } = require('../svgkit');

exports.id = 'arch-coverage-run';
exports.caption = 'A network coverage run. Routing finishes for every pair before any reference planner is asked.';

exports.svg = () => {
  const s = new Svg(800, 830);

  s.box('post',   230, 14, 340, 44, 'POST /network-coverage/runs', { kind: 'module', size: 12.5 });
  s.box('create', 150, 74, 500, 54, 'runner.create_run()', { kind: 'module', sub: 'validate · load hubs · enumerate pairs · INSERT pending' });
  s.text(776, 148, 'HTTP 200 returns here — the run is still “pending” and nothing has been routed yet',
    { size: 10.5, anchor: 'end', italic: true, fill: '#A9691A' });
  s.box('bg',     230, 158, 340, 48, 'bg.add_task(execute_run)', { kind: 'container', sub: 'in the web container', size: 12.5 });

  // ---- phase 1
  s.frame(20, 232, 760, 88, 'Phase 1 — snapshot and start');
  s.box('p1a',  40, 252, 230, 52, 'status → running', { kind: 'module', sub: 'config frozen for the run', size: 12 });
  s.box('p1b', 290, 252, 230, 52, 're-read active hubs', { kind: 'module', sub: 'country filter applied', size: 12 });
  s.box('p1c', 540, 252, 220, 52, 'enumerate pairs', { kind: 'module', sub: 'both → N·(N−1)', size: 12 });
  s.arrow(['p1a', 'r'], ['p1b', 'l'], {});
  s.arrow(['p1b', 'r'], ['p1c', 'l'], {});

  // ---- phase 2
  s.frame(20, 344, 760, 152, 'Phase 2 — route every pair');
  s.box('gather',  36, 384, 142, 76, ['gather', 'over pairs'], { kind: 'module', sub: 'semaphore', size: 12 });
  s.box('cancel', 190, 384, 132, 76, ['cancel', 'check'], { kind: 'muted', sub: 'memory + DB', size: 12 });
  s.box('kslot',  334, 384, 142, 76, ['K-slot', 'fan-out'], { kind: 'module', sub: 'K = 6', size: 12 });
  s.box('engine', 488, 384, 132, 76, ['otp /', 'motis'], { kind: 'engine', sub: 'per slot', size: 12 });
  s.box('persist',632, 384, 136, 76, ['persist', 'the cell'], { kind: 'store', sub: 'own txn', size: 12 });
  s.arrow(['gather', 'r'], ['cancel', 'l'], {});
  s.arrow(['cancel', 'r'], ['kslot', 'l'], {});
  s.arrow(['kslot', 'r'], ['engine', 'l'], {});
  s.arrow(['engine', 'r'], ['persist', 'l'], {});
  s.text(40, 480, 'One short-lived DB transaction per pair. A cancel is honoured between pairs, never mid-pair.', { size: 10.5 });

  // ---- phase 3
  s.frame(20, 520, 760, 164, 'Phase 3 — verify and finalise', { color: '#A9691A' });
  s.text(756, 548, 'skipped entirely if the run was cancelled', { size: 10.5, anchor: 'end', fill: '#A9691A', italic: true });
  s.box('sweep',  60, 556, 200, 70, ['reference sweep'], { kind: 'module', sub: 'over every cell', size: 12 });
  s.box('oebb',  300, 556, 200, 70, 'ÖBB HAFAS', { kind: 'external', sub: '≈ 1 request / second', size: 12 });
  s.box('align', 540, 556, 200, 70, 'compute_alignment', { kind: 'module', sub: 'score + tier per cell', size: 12 });
  s.arrow(['sweep', 'r'], ['oebb', 'l'], {});
  s.arrow(['oebb', 'r'], ['align', 'l'], {});
  s.text(40, 652, 'Eight tier strings are written: agree · mostly_agree · partial · disagree · no_overlap ·', { size: 10.5 });
  s.text(40, 668, 'one_sided_oebb · one_sided_viator · no_service.   A ninth, no_data, exists only in the matrix UI.', { size: 10.5 });

  s.box('done', 280, 706, 240, 46, 'status = completed', { kind: 'store', size: 12.5 });

  // vertical drops are kept clear of the dashed frame labels on the left
  s.arrow(['post', 'b'], ['create', 't'], {});
  s.arrow(['create', 'b', -230], ['bg', 't', -160], {});
  s.arrow(['bg', 'b'], ['p1a', 't', 80], { route: 'orth', mid: 224 });
  s.arrow(['p1c', 'b'], ['gather', 't'], { route: 'orth', mid: 330 });
  s.arrow(['persist', 'b'], ['sweep', 't', 80], { route: 'orth', mid: 508 });
  s.arrow(['align', 'b'], ['done', 't'], { route: 'orth', mid: 694 });

  s.step(1, 52, 396); s.step(2, 346, 396); s.step(3, 72, 568);

  s.text(24, 780, 'The whole run executes inside the web container as a FastAPI background task. Killing web kills the run in flight;', { size: 10.5 });
  s.text(24, 796, 'a startup hook marks any run left “running” as failed.', { size: 10.5 });
  s.text(24, 820, 'Phase 1 re-reads the hub list, so a hub edited between creating and starting a run changes what actually gets tested.',
    { size: 10.5, italic: true, fill: '#8A96A3' });

  return s.render();
};
