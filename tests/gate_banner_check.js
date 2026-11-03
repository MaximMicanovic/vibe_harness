/* Green-gate banner + card warnings + force-accept check (issue #14).
 *
 * Usage: node tests/gate_banner_check.js <path-to-app.js>
 *
 * Boots the served app.js under the shared DOM stub (tests/dom_stub.js) and
 * checks the spec:
 *
 *   - with test_cmd_configured=false in GET /api/settings, the board shows the
 *     amber #gate-banner ("No test command configured … gate is OFF") and the
 *     settings panel shows the same warning; with test_cmd_configured=true
 *     both stay hidden;
 *   - a done card carrying server `warnings` renders a .chip-card-warning per
 *     warning, and an exhausted review renders a .chip-review-exhausted that
 *     says the accept needs force;
 *   - clicking Accept on an exhausted card POSTs {"force": true} (after the
 *     confirm), while an approved card is accepted with no body — a bare
 *     accept of an exhausted card is never sent from the board.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const { makeDom } = require('./dom_stub.js');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: gate_banner_check.js <app.js>'); process.exit(2); }

/* ---------- the server the stub answers for ---------- */
const DONE_EXHAUSTED = {
  id: 'c1', title: 'stuck', body: 'b', status: 'done',
  created_at: '2026-11-03T10:00:00Z', updated_at: '2026-11-03T11:00:00Z',
  project: '', template: null, auto_run: false, auto_accept: false,
  depends_on: [], questions: [],
  warnings: ['no test command configured (test_cmd unset) — accepting this card is NOT gated by tests',
             'review rounds exhausted without an approval — accepting this card requires the explicit force override'],
  session: { review: { approved: false, exhausted: true, reason: 'bugs found' } },
};
const DONE_APPROVED = Object.assign({}, DONE_EXHAUSTED, {
  id: 'c2', title: 'good',
  warnings: ['no test command configured (test_cmd unset) — accepting this card is NOT gated by tests'],
  session: { review: { approved: true, exhausted: false } },
});
const CARDS = [DONE_EXHAUSTED, DONE_APPROVED];

function res(payload, status) {
  const code = status || 200;
  return {
    ok: code < 400, status: code, statusText: code < 400 ? 'OK' : 'Error',
    headers: { get: () => null },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

function makeFetch(settingsPayload, calls) {
  return async function fetchStub(path, options) {
    const p = String(path);
    const method = (options && options.method) || 'GET';
    calls.push({ url: p, method, body: options && options.body });
    if (p === '/api/settings') return res(settingsPayload);
    if (p === '/api/cards') return res(CARDS);
    if (p.startsWith('/api/cards/')) return res(CARDS.find((c) => p === '/api/cards/' + c.id) || {});
    if (p.startsWith('/api/projects')) return res([]);
    if (p.startsWith('/api/stacks')) return res([]);
    if (p.startsWith('/api/archives')) return res([]);
    if (p.startsWith('/api/models')) return res({ roles: {}, models: [], default_model: null });
    if (p.startsWith('/api/version')) return res({ version: 'test' });
    if (p.startsWith('/api/stats')) return res({ total_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0 });
    return res({});
  };
}

const SETTINGS_OFF = {
  backend: 'vibe', backends: [], failure_kinds: [], max_concurrent: 3, running: 0,
  accept_mode: 'local', accept_modes: [{ key: 'local', label: 'local' }],
  test_cmd_configured: false, test_cmd: null,
  prevent_sleep: false, sleep_held: false, sleep_supported: false,
};
const SETTINGS_ON = Object.assign({}, SETTINGS_OFF, {
  test_cmd_configured: true, test_cmd: '.venv/bin/python -m pytest -q',
});

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

async function bootWith(settings) {
  const calls = [];
  const dom = makeDom({ fetch: makeFetch(settings, calls) });
  const X = dom.boot(APP_JS, ['state', 'buildCard', 'renderSettingsPanel']);
  await dom.flush();
  return { dom, X, calls };
}

async function main() {
  /* --- 1. gate OFF: banner + settings warning + card warning chips --- */
  const off = await bootWith(SETTINGS_OFF);
  const banner = off.dom.getEl('gate-banner');
  check(banner.hidden === false, 'with test_cmd_configured=false the #gate-banner must be visible');
  check(/no test command configured/i.test(off.dom.getEl('gate-banner-text').textContent),
    'the banner text must say no test command is configured, got '
      + JSON.stringify(off.dom.getEl('gate-banner-text').textContent));

  await off.X.renderSettingsPanel();
  await off.dom.flush();
  const warn = off.dom.getEl('sidebar-body').querySelectorAll('.settings-warning');
  check(warn.length === 1, 'the settings panel must show one .settings-warning when the gate is off, got ' + warn.length);
  check(warn.length === 1 && /no test command configured/i.test(warn[0].textContent),
    'the settings warning must say no test command is configured');

  const card = off.X.buildCard(DONE_EXHAUSTED);
  const wchips = card.querySelectorAll('.chip-card-warning');
  check(wchips.length === 2, 'a done card with 2 server warnings must render 2 .chip-card-warning chips, got ' + wchips.length);
  check(wchips.length === 2 && /test command/i.test(wchips[0].title) && /force/i.test(wchips[1].title),
    'the warning chips keep the full server text as hover title');
  const exChip = card.querySelector('.chip-review-exhausted');
  check(!!exChip && /force/i.test(exChip.textContent),
    'the exhausted-review chip must say the accept needs force, got '
      + JSON.stringify(exChip && exChip.textContent));

  /* --- 2. Accept click on the exhausted card sends force:true --- */
  off.X.state.editorCardId = 'c1';
  for (const fn of (off.dom.getEl('btn-accept').listeners.click || [])) {
    await fn({ target: off.dom.getEl('btn-accept') });
  }
  await off.dom.flush();
  const acceptCalls = off.calls.filter((c) => /\/api\/cards\/c1\/accept$/.test(c.url) && c.method === 'POST');
  check(acceptCalls.length === 1, 'one accept POST expected for the exhausted card, got ' + acceptCalls.length);
  check(acceptCalls.length === 1 && acceptCalls[0].body && acceptCalls[0].body.indexOf('"force":true') !== -1,
    'accepting an exhausted card must POST {"force": true}, got ' + JSON.stringify(acceptCalls[0] && acceptCalls[0].body));

  /* --- 3. Accept click on the approved card sends no force --- */
  const off2 = await bootWith(SETTINGS_OFF);
  off2.X.state.editorCardId = 'c2';
  for (const fn of (off2.dom.getEl('btn-accept').listeners.click || [])) {
    await fn({ target: off2.dom.getEl('btn-accept') });
  }
  await off2.dom.flush();
  const acceptCalls2 = off2.calls.filter((c) => /\/api\/cards\/c2\/accept$/.test(c.url) && c.method === 'POST');
  check(acceptCalls2.length === 1, 'one accept POST expected for the approved card, got ' + acceptCalls2.length);
  check(acceptCalls2.length === 1 && !acceptCalls2[0].body,
    'accepting an approved card must NOT send force, got ' + JSON.stringify(acceptCalls2[0] && acceptCalls2[0].body));

  /* --- 4. gate ON: banner and settings warning stay hidden --- */
  const on = await bootWith(SETTINGS_ON);
  check(on.dom.getEl('gate-banner').hidden === true,
    'with test_cmd_configured=true the #gate-banner must stay hidden');
  await on.X.renderSettingsPanel();
  await on.dom.flush();
  check(on.dom.getEl('sidebar-body').querySelectorAll('.settings-warning').length === 0,
    'no .settings-warning in the settings panel when the gate is armed');

  if (failures.length) {
    console.log(JSON.stringify({ ok: false, failures }));
    process.exit(1);
  }
  console.log(JSON.stringify({ ok: true, failures: [] }));
}

main().catch((err) => { console.error(err); process.exit(2); });
