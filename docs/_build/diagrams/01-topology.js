/* Runtime process topology.
 * Facts from facts-topology.md, corrected by the adversarial pass:
 *   - the graphs volume has SIX mounts at FOUR distinct container paths
 *     (/data/graphs, /var/otp/graph, /var/motis-graphs, /graphs)
 *   - outbound SMTP from `web` was missing and is the only egress carrying PII
 * The three claims this diagram exists to get right, because prose keeps
 * getting them wrong:
 *   - coverage runs and ALL outbound traffic happen in `web`, not `worker`
 *   - `web` has no docker socket; it hands config to `worker` through a file
 *   - serve containers are dialled directly by DNS, not through nginx
 */
const { Svg } = require('../svgkit');

exports.id = 'arch-topology';
exports.caption = 'Runtime process topology. One host, one Docker project. Only nginx is reachable from outside.';

exports.svg = () => {
  const s = new Svg(832, 566);

  // ---- the operator, outside everything
  s.box('browser', 402, 14, 240, 42, 'Operator’s browser', { kind: 'actor' });

  // ---- external systems, kept on the left so the arrows out of `web` are short
  s.box('ojp',  20, 180, 180, 48, 'Swiss OJP 2.0', { kind: 'external', sub: 'opentransportdata.swiss' });
  s.box('oebb', 20, 246, 180, 48, 'ÖBB HAFAS',   { kind: 'external', sub: 'fahrplan.oebb.at' });
  s.box('naps', 20, 312, 180, 52, ['National Access Points', 'OSM extracts'], { kind: 'external' });
  s.box('smtp', 20, 380, 180, 44, 'SMTP', { kind: 'external', sub: 'account mail' });

  // ---- the docker project
  s.frame(237, 76, 578, 440, 'Docker project “viator” — single host');

  s.box('nginx', 392, 96, 250, 44, 'nginx', { kind: 'container', sub: ':80 → 301  ·  :443 TLS' });
  s.box('web',   257, 166, 230, 78, 'web',  { kind: 'container', sub: 'uvicorn + FastAPI', badge: 'coverage runs here' });
  s.box('worker',567, 166, 230, 78, 'worker',{ kind: 'container', sub: 'same image, other command' });
  s.box('pg',    413, 280, 220, 46, 'postgres', { kind: 'store', sub: 'jobs · sessions · results' });
  s.box('serve', 257, 360, 230, 76, ['otp-<sid>  /  motis-<sid>'], { kind: 'engine', sub: '0..N  ·  :8080' });
  s.box('dockerd',567, 360, 230, 46, 'host docker daemon', { kind: 'muted', sub: '/var/run/docker.sock' });
  s.box('graphs',257, 455, 230, 44, 'graphs volume', { kind: 'store', sub: '6 mounts · 4 paths' });
  s.box('inbox', 567, 455, 230, 44, 'inbox volume', { kind: 'store', sub: 'feeds · PBFs' });

  // ---- traffic in
  s.arrow(['browser', 'b'], ['nginx', 't'], { label: 'HTTPS 443' });
  s.arrow(['nginx', 'b', -60], ['web', 't', -55], { route: 'orth', label: 'HTTP :8000', ly: 158 });

  // ---- web is the only thing that talks to the outside world.
  // Nested mids keep the four lines in the 200..237 channel, left of the frame.
  s.arrow(['web', 'l', -24], ['ojp', 'r'],  { route: 'orth', mid: 230, color: '#A9691A' });
  s.arrow(['web', 'l', -8],  ['oebb', 'r'], { route: 'orth', mid: 222, color: '#A9691A' });
  s.arrow(['web', 'l', 8],   ['naps', 'r'], { route: 'orth', mid: 214, color: '#A9691A' });
  s.arrow(['web', 'l', 24],  ['smtp', 'r'], { route: 'orth', mid: 206, color: '#A9691A' });
  s.text(207, 142, 'every outbound connection', { size: 10.5, anchor: 'end', fill: '#A9691A', italic: true });
  s.text(207, 155, 'originates in web', { size: 10.5, anchor: 'end', fill: '#A9691A', italic: true });

  // ---- the config handoff: web writes, worker applies
  s.arrow(['web', 'r'], ['worker', 'l'], {
    dashed: true, label: ['config files +', 'reload trigger'], ly: 196,
  });

  // ---- routing traffic, straight to the container by DNS
  s.arrow(['web', 'b', -70], ['serve', 't', -70], {});
  s.text(298, 300, 'HTTP :8080', { size: 10.5, anchor: 'end' });
  s.text(298, 313, 'direct DNS,', { size: 10.5, anchor: 'end' });
  s.text(298, 326, 'not via nginx', { size: 10.5, anchor: 'end', fill: '#A9691A' });

  // ---- persistence
  s.arrow(['web', 'b', 60], ['pg', 't', -55], {});
  s.arrow(['worker', 'b', -60], ['pg', 't', 55], {});

  // ---- the build lane
  s.arrow(['worker', 'b', 70], ['dockerd', 't', 70], {});
  s.text(690, 300, 'spawns one-shot', { size: 10.5, anchor: 'start' });
  s.text(690, 313, 'build containers', { size: 10.5, anchor: 'start' });
  s.arrow(['dockerd', 'b', 60], ['inbox', 't', 60], { soft: true });
  s.arrow(['dockerd', 'l', -12], ['serve', 'r', 8], { dashed: true, route: 'orth', mid: 527, label: 'starts / stops', ly: 372 });
  s.arrow(['inbox', 'l'], ['graphs', 'r'], { color: '#2E6B3E', label: 'build writes', ly: 472 });
  // data flows volume -> container, so the arrow points up into the engine
  s.arrow(['graphs', 't'], ['serve', 'b'], { color: '#2E6B3E' });
  s.text(378, 449, 'mounted read-only', { size: 10.5, anchor: 'start', fill: '#2E6B3E' });

  s.legend(24, 540, [
    { kind: 'container', label: 'VIATOR process' },
    { kind: 'engine', label: 'routing engine' },
    { kind: 'store', label: 'persistent state' },
    { kind: 'external', label: 'third party' },
    { kind: 'actor', label: 'person' },
  ], { gap: 158 });

  return s.render();
};
