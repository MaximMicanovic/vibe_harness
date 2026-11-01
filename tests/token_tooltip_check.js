/* Token-counter hover-tooltip reproducer for note 240088.
 *
 * Usage: node tests/token_tooltip_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub, then checks the spec:
 *
 *   "in the counter when I hover my mouse on it I want it to show me what
 *    types of tokens it was. Cached, output, input."
 *
 * Concretely:
 *   - the top-bar token counter (#stat-tokens-wrap) gets a hover title that
 *     names the token types with their counts (cached, output, input) when
 *     /api/stats carries token_breakdown, and no type names when it doesn't;
 *   - a card's token chip gets the same treatment from the note session's
 *     token_breakdown (recorded) / live_token_breakdown (live ⚡ chip).
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: token_tooltip_check.js <app.js>'); process.exit(2); }

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
    scrollTop: 0, scrollWidth: 0,
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
vm.runInContext(src + '\n;globalThis.__x = { state, renderStats, buildCard, fmtTokens };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.renderStats !== 'function' || typeof X.buildCard !== 'function') {
  console.error('could not load renderStats/buildCard from app.js');
  process.exit(2);
}

/* ---------- checks ---------- */
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

function note(over) {
  return Object.assign({
    id: 'n1', title: 'a note', body: 'body', status: 'done',
    created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T11:00:00Z',
    project: '', template: null, auto_run: false, auto_accept: false,
    depends_on: [], session: null, questions: [],
  }, over);
}

/* 1. Top-bar counter: with a breakdown in /api/stats, hovering the counter
 *    names the token types and their counts — cached, output, input. */
X.state.stats = {
  counts: {}, total_tokens: 6942, live_tokens: 0,
  token_breakdown: { cached: 3072, output: 28, input: 6914 },
  total_runtime_seconds: 0, avg_runtime_seconds: 0, project: null,
};
X.renderStats();
const wrap = getEl('stat-tokens-wrap');
check(getEl('stat-tokens').textContent === '6.9k', 'counter shows the total, got ' + JSON.stringify(getEl('stat-tokens').textContent));
check(typeof wrap.title === 'string' && wrap.title.length > 0, 'top-bar counter has a hover title, got ' + JSON.stringify(wrap.title));
for (const [label, val] of [['cached', '3.1k'], ['output', '28'], ['input', '6.9k']]) {
  check(wrap.title && wrap.title.indexOf(label) !== -1 && wrap.title.indexOf(val) !== -1,
    'top-bar hover title should mention ' + label + ' ' + val + ', got ' + JSON.stringify(wrap.title));
}

/* 2. Without a breakdown, the hover title must not invent type numbers. */
X.state.stats = { counts: {}, total_tokens: 42, live_tokens: 0, token_breakdown: { cached: null, output: null, input: null }, total_runtime_seconds: 0, avg_runtime_seconds: 0, project: null };
X.renderStats();
check(!/cached|output|input/.test(getEl('stat-tokens-wrap').title || ''),
  'no breakdown -> no type names in the hover title, got ' + JSON.stringify(getEl('stat-tokens-wrap').title));
X.state.stats = { counts: {}, total_tokens: 42, live_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0, project: null };
X.renderStats();
check(!/cached|output|input/.test(getEl('stat-tokens-wrap').title || ''),
  'missing breakdown key -> no type names in the hover title, got ' + JSON.stringify(getEl('stat-tokens-wrap').title));

/* 3. Card chip (recorded tokens): hover shows the session's token types. */
const card = X.buildCard(note({ session: { tokens: 100, token_breakdown: { cached: 10, output: 20, input: 70 } } }));
const tokChip = card.querySelector('.chip-tokens');
check(!!tokChip, 'a token chip renders on the card');
if (tokChip) {
  // Round 2 of this note: the little bubble itself carries the three types;
  // the session total (100) moved into the hover title.
  check(tokChip.title && tokChip.title.indexOf('100') !== -1,
    'chip hover title should carry the session total 100, got ' + JSON.stringify(tokChip.title));
  for (const [label, val] of [['cached', '10'], ['output', '20'], ['input', '70']]) {
    check(tokChip.title && tokChip.title.indexOf(label) !== -1 && tokChip.title.indexOf(val) !== -1,
      'token chip hover title should mention ' + label + ' ' + val + ', got ' + JSON.stringify(tokChip.title));
  }
}

/* 4. Card chip (recorded tokens, no breakdown): no invented type numbers. */
const cardNoBd = X.buildCard(note({ id: 'n2', session: { tokens: 100 } }));
const tokChipNoBd = cardNoBd.querySelector('.chip-tokens');
check(!!tokChipNoBd && !/cached|output|input/.test(tokChipNoBd.title || ''),
  'no breakdown -> token chip hover title has no type names, got ' + JSON.stringify(tokChipNoBd && tokChipNoBd.title));

/* 5. Live ⚡ chip while running: hover shows the live breakdown. */
const liveCard = X.buildCard(note({
  id: 'n3', status: 'running',
  session: { started_at: '2026-10-29T10:00:00Z', live_tokens: 500,
             live_token_breakdown: { cached: 100, output: 200, input: 200 } },
}));
const liveChip = liveCard.querySelector('.tokens-live');
check(!!liveChip, 'a live token chip renders on a running card');
if (liveChip) {
  // Round 2: the live bubble shows the three live types; the live total (500)
  // moved to the hover title.
  check(liveChip.title && liveChip.title.indexOf('500') !== -1,
    'live chip hover title should carry the live total 500, got ' + JSON.stringify(liveChip.title));
  for (const [label, val] of [['cached', '100'], ['output', '200'], ['input', '200']]) {
    check(liveChip.title && liveChip.title.indexOf(label) !== -1 && liveChip.title.indexOf(val) !== -1,
      'live chip hover title should mention ' + label + ' ' + val + ', got ' + JSON.stringify(liveChip.title));
  }
}

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);
