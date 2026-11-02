/* Token-type bubble reproducer for note 240088 (round 2).
 *
 * Usage: node tests/token_bubbles_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub, then checks the spec:
 *
 *   "Instead of having one counter for tokens in total I want cached, input,
 *    output in the same little bubble. The same for total tokens but there we
 *    have more space so we can have three bubbles."
 *
 * Concretely:
 *   - the per-card token chip (the little bubble) shows cached, input and
 *     output *in the same single chip* when the session carries a breakdown
 *     (recorded token_breakdown / live_token_breakdown), falling back to the
 *     plain "N tok" counter when it doesn't;
 *   - the top-bar total-tokens stat renders *three separate bubbles* — one
 *     each for cached, input, output — next to the total when /api/stats
 *     carries token_breakdown, and no type bubbles when it doesn't;
 *   - hover titles keep naming the token types with their counts.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: token_bubbles_check.js <app.js>'); process.exit(2); }

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

/* 1. Top bar, with a breakdown: THREE separate bubbles — cached, input,
 *    output — next to the total, which still renders and still ticks. */
X.state.stats = {
  counts: {}, total_tokens: 6942, live_tokens: 0,
  token_breakdown: { cached: 3072, output: 28, input: 6914 },
  total_runtime_seconds: 0, avg_runtime_seconds: 0, project: null,
};
X.renderStats();
const wrap = getEl('stat-tokens-wrap');
const types = getEl('stat-tok-types');
check(getEl('stat-tokens').textContent === '6.9k',
  'total still shows in the top bar, got ' + JSON.stringify(getEl('stat-tokens').textContent));
const bubbles = types ? types.querySelectorAll('.stat-tok') : [];
check(bubbles.length === 3, 'top bar should render exactly three token-type bubbles, got ' + bubbles.length);
for (const [cls, label, val] of [['tok-cached', 'cached', '3.1k'], ['tok-input', 'input', '6.9k'], ['tok-output', 'output', '28']]) {
  const b = types.querySelector('.' + cls);
  check(!!b, 'missing ' + cls + ' bubble');
  if (b) {
    check(b.textContent.indexOf(label) !== -1 && b.textContent.indexOf(val) !== -1,
      cls + ' bubble should read "' + label + ' ' + val + '", got ' + JSON.stringify(b.textContent));
  }
}
// Hover on the top-bar stat still names the types with counts.
for (const [label, val] of [['cached', '3.1k'], ['output', '28'], ['input', '6.9k']]) {
  check(wrap.title && wrap.title.indexOf(label) !== -1 && wrap.title.indexOf(val) !== -1,
    'top-bar hover title should mention ' + label + ' ' + val + ', got ' + JSON.stringify(wrap.title));
}

/* 2. Top bar, breakdown all null / missing: no type bubbles, total remains. */
for (const bd of [{ cached: null, output: null, input: null }, undefined]) {
  X.state.stats = { counts: {}, total_tokens: 42, live_tokens: 0, token_breakdown: bd, total_runtime_seconds: 0, avg_runtime_seconds: 0, project: null };
  X.renderStats();
  const t = getEl('stat-tok-types');
  check(getEl('stat-tokens').textContent === '42', 'total should still render without a breakdown');
  check((!t || t.querySelectorAll('.stat-tok').length === 0),
    'no breakdown -> no token-type bubbles in the top bar, got ' + (t ? t.querySelectorAll('.stat-tok').length : 'n/a'));
  check(!/cached|output|input/.test(getEl('stat-tokens-wrap').title || ''),
    'no breakdown -> no type names in the hover title, got ' + JSON.stringify(getEl('stat-tokens-wrap').title));
}

/* 3. Card chip (recorded tokens) with a breakdown: cached, input and output
 *    all in the SAME single little bubble. */
const card = X.buildCard(note({ session: { tokens: 100, token_breakdown: { cached: 10, output: 20, input: 70 } } }));
const tokChips = card.querySelectorAll('.chip-tokens');
check(tokChips.length === 1, 'the breakdown must stay in ONE little bubble, got ' + tokChips.length + ' token chips');
if (tokChips.length === 1) {
  const text = tokChips[0].textContent;
  for (const [label, val] of [['cached', '10'], ['input', '70'], ['output', '20']]) {
    check(text.indexOf(label) !== -1 && text.indexOf(val) !== -1,
      'card bubble should contain "' + label + ' ' + val + '", got ' + JSON.stringify(text));
  }
  for (const [label, val] of [['cached', '10'], ['output', '20'], ['input', '70']]) {
    check(tokChips[0].title && tokChips[0].title.indexOf(label) !== -1 && tokChips[0].title.indexOf(val) !== -1,
      'card chip hover title should still mention ' + label + ' ' + val + ', got ' + JSON.stringify(tokChips[0].title));
  }
}

/* 4. Card chip without a breakdown: falls back to the plain total counter. */
const cardNoBd = X.buildCard(note({ id: 'n2', session: { tokens: 100 } }));
const tokChipNoBd = cardNoBd.querySelector('.chip-tokens');
check(!!tokChipNoBd && tokChipNoBd.textContent === '100 tok',
  'no breakdown -> chip falls back to "100 tok", got ' + JSON.stringify(tokChipNoBd && tokChipNoBd.textContent));
check(!!tokChipNoBd && !/cached|output|input/.test(tokChipNoBd.title || ''),
  'no breakdown -> chip hover title has no type names, got ' + JSON.stringify(tokChipNoBd && tokChipNoBd.title));

/* 5. Live ⚡ chip while running: cached, input, output in the same bubble. */
const liveCard = X.buildCard(note({
  id: 'n3', status: 'running',
  session: { started_at: '2026-10-29T10:00:00Z', live_tokens: 500,
             live_token_breakdown: { cached: 100, output: 200, input: 200 } },
}));
const liveChips = liveCard.querySelectorAll('.tokens-live');
check(liveChips.length === 1, 'the live breakdown must stay in ONE little bubble, got ' + liveChips.length + ' live chips');
if (liveChips.length === 1) {
  const text = liveChips[0].textContent;
  for (const [label, val] of [['cached', '100'], ['input', '200'], ['output', '200']]) {
    check(text.indexOf(label) !== -1 && text.indexOf(val) !== -1,
      'live bubble should contain "' + label + ' ' + val + '", got ' + JSON.stringify(text));
  }
  check(liveChips[0].title && liveChips[0].title.indexOf('500') !== -1,
    'live chip hover title should still carry the live total 500, got ' + JSON.stringify(liveChips[0].title));
  for (const [label, val] of [['cached', '100'], ['output', '200'], ['input', '200']]) {
    check(liveChips[0].title && liveChips[0].title.indexOf(label) !== -1 && liveChips[0].title.indexOf(val) !== -1,
      'live chip hover title should mention ' + label + ' ' + val + ', got ' + JSON.stringify(liveChips[0].title));
  }
}

/* 6. The chip shows the card's TOTAL cost (worker + review + resolver
 *    rounds), not the worker's export total alone. */
const totalCard = X.buildCard(note({
  id: 'n4',
  session: { tokens: 100, card_tokens: 250,
             card_token_breakdown: { cached: 30, output: 20, input: 200 } },
}));
const totalChip = totalCard.querySelector('.chip-tokens');
check(!!totalChip, 'a token chip renders for a card with a total');
if (totalChip) {
  check(totalChip.title && totalChip.title.indexOf('250') !== -1,
    'chip hover title should carry the card total 250, got ' + JSON.stringify(totalChip.title));
  check(totalChip.title && totalChip.title.indexOf('100') === -1,
    'chip hover title must not show the worker-only total, got ' + JSON.stringify(totalChip.title));
  for (const [label, val] of [['cached', '30'], ['output', '20'], ['input', '200']]) {
    check(totalChip.title && totalChip.title.indexOf(label) !== -1 && totalChip.title.indexOf(val) !== -1,
      'card total chip hover title should mention ' + label + ' ' + val + ', got ' + JSON.stringify(totalChip.title));
  }
}

/* 7. Card total without a breakdown: the plain total counter. */
const totalNoBd = X.buildCard(note({ id: 'n5', session: { tokens: 100, card_tokens: 250 } }));
const totalNoBdChip = totalNoBd.querySelector('.chip-tokens');
check(!!totalNoBdChip && totalNoBdChip.textContent === '250 tok',
  'card total without a breakdown -> "250 tok", got ' + JSON.stringify(totalNoBdChip && totalNoBdChip.textContent));

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);
