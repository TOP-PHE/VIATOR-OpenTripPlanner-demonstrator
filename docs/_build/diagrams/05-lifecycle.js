/* Session state machine + how a compiled graph becomes live.
 * Facts from facts-lifecycle.md (app/models/sessions.py:27-34 for the verbatim
 * state strings, app/worker.py and app/api/admin/sessions.py for transitions).
 * Deliberately shows the two states that exist in the enum but are never
 * assigned, and the unvalidated PATCH escape hatch — both are real and both
 * surprise people.
 */
const { Svg } = require('../svgkit');

exports.id = 'arch-session-lifecycle';
exports.caption = 'Session state machine, and the path a compiled graph takes before it can answer a search.';

exports.svg = () => {
  const s = new Svg(800, 566);

  s.text(24, 26, 'Session states', { size: 14, weight: 700, fill: '#1F3864' });
  s.text(24, 44, 'Only the web container writes states, with one exception: the worker performs populated → graph_built.', { size: 10.5 });

  // ---- ghost state: in the enum, never assigned
  s.box('configured', 180, 74, 112, 40, 'configured', { kind: 'muted', dashed: true, size: 12 });
  s.text(236, 128, 'declared, never assigned', { size: 9.5, anchor: 'middle', italic: true, fill: '#8A96A3' });

  // ---- the states that actually occur
  s.box('created',   24, 150, 112, 58, 'created',    { kind: 'state' });
  s.box('populated',180, 150, 112, 58, 'populated',  { kind: 'state' });
  s.box('graphbuilt',336, 150, 124, 58, 'graph_built',{ kind: 'state' });
  s.box('serving',  512, 150, 112, 58, 'serving',    { kind: 'state', bold: true });
  s.box('archived', 668, 150, 112, 58, 'archived',   { kind: 'muted' });

  // labels sit above the row; the inter-box gaps are narrower than the text
  s.arrow(['created', 'r'], ['populated', 'l'], { label: ['upload / refresh', 'web'], ly: 143 });
  s.arrow(['populated', 'r'], ['graphbuilt', 'l'], { label: ['build succeeded', 'worker'], ly: 143, color: '#A9691A' });
  s.arrow(['graphbuilt', 'r'], ['serving', 'l'], { label: ['POST /promote', 'web'], ly: 143 });
  s.arrow(['serving', 'r'], ['archived', 'l'], { label: ['POST /archive', 'web'], ly: 143, soft: true });

  // promote is a legal re-trigger on an already-serving session
  s.arrow(['serving', 't', 30], ['serving', 't', -30], { route: 'orth', mid: 126, soft: true });
  s.text(568, 120, 're-promote allowed', { size: 9.5, anchor: 'middle', italic: true, fill: '#8A96A3' });

  // the escape hatch
  s.arrow(['archived', 'b'], ['created', 'b'], {
    route: 'orth', mid: 248, dashed: true, markerStart: true, color: '#A9691A',
    label: 'PATCH /{sid} sets any state to any other — no transition validation', ly: 244,
  });

  // what actually makes a session answerable
  s.frame(496, 268, 284, 48, null, { color: '#2E6B3E', fill: '#F2F8F4', solid: true });
  s.text(638, 288, 'A session answers a search only when', { size: 10.5, anchor: 'middle', fill: '#1E4A2B' });
  s.text(638, 303, "state='serving' AND include_in_fanout", { size: 10.5, anchor: 'middle', fill: '#1E4A2B', mono: true });

  // ---- band 2: from build to live
  s.text(24, 358, 'How a compiled graph becomes live', { size: 14, weight: 700, fill: '#1F3864' });

  const steps = [
    { id: 's1', t: ['web enqueues', 'a rebuild job'], sub: 'status=pending' },
    { id: 's2', t: ['worker claims it,', 'runs the build'], sub: 'after debounce' },
    { id: 's3', t: ['graph lands in', '<sid>/<timestamp>/'], sub: 'worker moves it' },
    { id: 's4', t: ['current  →', '<timestamp>'], sub: 'relative symlink' },
    { id: 's5', t: ['promote swaps', 'the container'], sub: '≤ 15 s' },
  ];
  steps.forEach((st, i) => {
    const x = 24 + i * 152;
    s.box(st.id, x, 396, 144, 76, st.t, { kind: i === 3 ? 'engine' : 'module', sub: st.sub, size: 12 });
    s.step(i + 1, x + 12, 390);
    if (i) s.arrow([steps[i - 1].id, 'r'], [st.id, 'l'], {});
  });

  s.text(24, 500, 'The symlink target must be relative: the graphs volume is mounted six times, at four different container paths,', { size: 10.5 });
  s.text(24, 515, 'so an absolute target would resolve only in the namespace that wrote it.', { size: 10.5 });
  s.text(24, 540, 'deleted is also in the enum and never assigned — DELETE hard-deletes the row instead.', { size: 10.5, italic: true, fill: '#8A96A3' });

  return s.render();
};
