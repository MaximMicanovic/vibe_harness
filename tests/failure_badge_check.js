/* Failure badge + board filter reproducer.
 *
 * Usage: node tests/failure_badge_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   "Say WHY a card failed, instead of making me read the log."
 *
 * Concretely:
 *   - a card whose session carries `failure` renders a badge naming the kind,
 *     with the classifier's detail as the hover title;
 *   - a card without a failure renders no badge;
 *   - the board filter (state.failureFilter) narrows the board to cards that
 *     failed that way, and '' shows everything again;
 *   - the filter select offers every kind the classifier can return.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) {
  console.error('usage: failure_badge_check.js <app.js> [classifier-kinds-json]');
  process.exit(2);
}
/* The classifier's kinds, handed over by the Python test (app/failures.py::KINDS)
 * so this check compares app.js against the real contract, not a copy. */
const CLASSIFIER_KINDS = process.argv[3]
  ? JSON.parse(process.argv[3])
  : ['stopped_by_user', 'interrupted', 'git_conflict', 'timeout', 'tests_failed',
     'reviewer_rejected', 'no_verdict', 'crashed'];

/* ---------- minimal DOM stub (enough for app.js to boot) ---------- */
function makeClassList() {
  const set = new Set();
  return {
    add: (...cs) => cs.forEach((c) => set.add(c)),
    remove: (...cs) => cs.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
    toggle: (c) => (set.has(c) ? (set.delete(c), false) : (set.add(c), true)),
    _set: set,
  };
}
function matchSimple(n, sel) {
  if (!n.classList) return false;
  if (sel.startsWith('.')) return sel.slice(1).split('.').every((c) => n.classList.contains(c));
  return n.tagName === sel.toUpperCase();
}
function matchComplex(n, parts) {
  if (!matchSimple(n, parts[parts.length - 1])) return false;
  let anc = n.parentNode;
  for (let i = parts.length - 2; i >= 0; i--) {
    while (anc && !matchSimple(anc, parts[i])) anc = anc.parentNode;
    if (!anc) return false;
    anc = anc.parentNode;
  }
  return true;
}
function makeEl(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [], childNodes: [], parentNode: null, listeners: {},
    dataset: {}, attributes: {}, hidden: false, draggable: false,
    offsetHeight: 140, scrollHeight: 20, clientHeight: 20,
    scrollTop: 0, scrollWidth: 0, value: '',
  };
  node.style = new Proxy({ _props: {} }, {
    get(t, k) {
      if (k === 'setProperty') return (kk, vv) => { t._props[kk] = vv; t[kk] = vv; };
      if (k === 'getPropertyValue') return (kk) => (t._props[kk] !== undefined ? t._props[kk] : '');
      return t[k];
    },
    set(t, k, v) { t[k] = v; return true; },
  });
  node.classList = makeClassList();
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList._set.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => node.classList._set.add(c)); },
  });
  node.appendChild = (c) => { c.parentNode = node; node.children.push(c); node.childNodes.push(c); return c; };
  node.removeChild = (c) => { node.children = node.children.filter((x) => x !== c); node.childNodes = node.childNodes.filter((x) => x !== c); };
  node.remove = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  Object.defineProperty(node, 'textContent', {
    get() { return node._text || ''; },
    set(v) { node._text = String(v); if (v === '') { node.children = []; node.childNodes = []; } },
  });
  node.setAttribute = (k, v) => { node.attributes[k] = String(v); };
  node.getAttribute = (k) => node.attributes[k];
  node.addEventListener = (type, fn) => { (node.listeners[type] = node.listeners[type] || []).push(fn); };
  node.removeEventListener = () => {};
  node.querySelector = (sel) => node.querySelectorAll(sel)[0] || null;
  node.querySelectorAll = (sel) => {
    const parts = sel.split(/\s+/);
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (matchComplex(c, parts)) out.push(c); walk(c); } };
    walk(node);
    return out;
  };
  node.closest = (sel) => { let n = node; while (n) { if (n.classList && matchSimple(n, sel)) return n; n = n.parentNode; } return null; };
  node.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 140, right: 200, bottom: 140 });
  node.focus = () => {};
  return node;
}

const byId = {};
const getEl = (id) => { if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; } return byId[id]; };
const store = {};
const sandbox = {
  console,
  document: {
    getElementById: getEl,
    createElement: (t) => makeEl(t),
    createElementNS: (ns, t) => makeEl(t),
    createTextNode: (t) => { const n = makeEl('#text'); n.textContent = t; return n; },
    addEventListener: () => {},
    body: makeEl('body'),
  },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  fetch: async () => ({ ok: true, status: 200, statusText: 'OK', json: async () => ({}), text: async () => '' }),
  requestAnimationFrame: (fn) => fn(),
  getComputedStyle: () => ({ fontSize: '15px' }),
  CSS: { escape: (s) => String(s) },
  navigator: { platform: 'test', userAgentData: null },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  Date, Math, JSON, Promise, Set, Map, Array, Object, String, Number, Boolean, parseFloat, parseInt, isNaN, RegExp, Error,
};
sandbox.addEventListener = () => {};
sandbox.confirm = () => true;
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(APP_JS, 'utf8');
vm.runInContext(src + '\n;globalThis.__x = { state, buildCard, visibleCards, FAILURE_LABELS, renderFailureFilter };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.buildCard !== 'function' || typeof X.visibleCards !== 'function') {
  console.error('could not load buildCard/visibleCards from app.js');
  process.exit(2);
}

/* ---------- checks ---------- */
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

function card(over) {
  return Object.assign({
    id: 'c1', title: 'a card', body: 'body', status: 'failed',
    created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T11:00:00Z',
    project: '', template: null, auto_run: false, auto_accept: false,
    depends_on: [], session: null, questions: [],
  }, over);
}

/* 1. The badge: kind on the card, detail in the tooltip. */
const failed = card({
  session: { exit_code: 0, verified: false,
             failure: { kind: 'tests_failed', detail: 'tests failed after 3 rounds' } },
});
const failedEl = X.buildCard(failed);
const badge = failedEl.querySelector('.tag-failure');
check(!!badge, 'a failed card renders a failure badge');
if (badge) {
  check(/tests failed/i.test(badge.textContent),
    'the badge names the kind, got ' + JSON.stringify(badge.textContent));
  check(badge.title === 'tests failed after 3 rounds',
    'the badge tooltip is the classifier detail, got ' + JSON.stringify(badge.title));
  check(badge.dataset.failureKind === 'tests_failed',
    'the badge carries the raw kind for filtering, got ' + JSON.stringify(badge.dataset.failureKind));
}

/* 2. Every kind has a label, and the badge shows it. */
const KINDS = CLASSIFIER_KINDS;
for (const kind of KINDS) {
  check(typeof X.FAILURE_LABELS[kind] === 'string' && X.FAILURE_LABELS[kind].length > 0,
    'FAILURE_LABELS has a label for ' + kind);
  const el = X.buildCard(card({
    id: 'k-' + kind,
    session: { failure: { kind: kind, detail: 'because ' + kind } },
  }));
  const b = el.querySelector('.tag-failure');
  check(!!b && b.textContent.indexOf(X.FAILURE_LABELS[kind]) !== -1,
    'the badge for ' + kind + ' shows its label');
  check(!!b && b.title === 'because ' + kind,
    'the badge for ' + kind + ' carries the detail as its tooltip');
}

/* 3. A card that did not fail has no badge. */
const okEl = X.buildCard(card({ id: 'c2', status: 'done', session: { exit_code: 0, verified: true } }));
check(!okEl.querySelector('.tag-failure'), 'a card without a failure renders no badge');

/* 4. The board filter: only failures of the chosen kind. */
X.state.cards = [
  card({ id: 'a', session: { failure: { kind: 'tests_failed', detail: 'x' } } }),
  card({ id: 'b', session: { failure: { kind: 'crashed', detail: 'y' } } }),
  card({ id: 'c', status: 'done', session: { exit_code: 0 } }),
];
X.state.selectedProject = '';
X.state.failureFilter = '';
check(X.visibleCards().length === 3, 'no filter -> every card is visible');
X.state.failureFilter = 'tests_failed';
const onlyTests = X.visibleCards().map((n) => n.id);
check(onlyTests.length === 1 && onlyTests[0] === 'a',
  'filtering by tests_failed shows only that card, got ' + JSON.stringify(onlyTests));
X.state.failureFilter = 'crashed';
const onlyCrashed = X.visibleCards().map((n) => n.id);
check(onlyCrashed.length === 1 && onlyCrashed[0] === 'b',
  'filtering by crashed shows only that card, got ' + JSON.stringify(onlyCrashed));
X.state.failureFilter = 'timeout';
check(X.visibleCards().length === 0, 'a kind nobody failed with shows nothing');
X.state.failureFilter = '';

/* 5. Before the settings land, the filter falls back to the classifier's kinds
 *    (the same list, in the same order) so the first paint is usable. */
X.state.settings = null;
X.renderFailureFilter();
const select = getEl('failure-filter');
const values = select.children.map((o) => o.value);
check(values[0] === '', 'the filter starts with "any reason", got ' + JSON.stringify(values));
check(JSON.stringify(values) === JSON.stringify([''].concat(KINDS)),
  'the fallback filter offers exactly the classifier kinds, got ' + JSON.stringify(values));

/* 6. Once GET /api/settings lands, its `failure_kinds` is the list — app.js
 *    keeps no copy of its own, so a new kind shows up without a JS edit. */
X.state.settings = { failure_kinds: ['crashed', 'timeout'] };
X.renderFailureFilter();
const fromApi = getEl('failure-filter').children.map((o) => o.value);
check(JSON.stringify(fromApi) === JSON.stringify(['', 'crashed', 'timeout']),
  'the filter offers exactly the kinds the API published, got ' + JSON.stringify(fromApi));
X.state.settings = null;
X.renderFailureFilter();

/* 7. A stored filter for a kind the API no longer offers is dropped, not left
 *    narrowing the board to nothing with no option to undo it. */
X.state.settings = { failure_kinds: ['crashed'] };
X.state.failureFilter = 'timeout';
X.renderFailureFilter();
check(X.state.failureFilter === '',
  'a filter for a kind the API no longer offers is cleared, got ' + JSON.stringify(X.state.failureFilter));
check(getEl('failure-filter').value === '',
  'the select shows "any reason" again, got ' + JSON.stringify(getEl('failure-filter').value));
X.state.settings = null;
X.state.failureFilter = '';
X.renderFailureFilter();

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);
