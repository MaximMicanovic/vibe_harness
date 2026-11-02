/* Diff-stat chip check for card 26671e (the growth signal on a card).
 *
 * Usage: node tests/diff_stat_chip_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   "Record `session.diff_stat` (lines added / removed ...) when a run
 *    finalizes. Show it as a card chip (e.g. `+412 −38`). A card that adds 900
 *    lines for a small spec becomes visible at a glance instead of only in
 *    review."
 *
 * Concretely:
 *   - a card whose session carries a diff_stat renders ONE chip reading
 *     "+412 −38" (added first, removed second, the minus is U+2212);
 *   - the hover title names the file count and says what the numbers are;
 *   - a card with no diff_stat (sandbox run, or a run recorded before this
 *     existed) renders no such chip at all.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: diff_stat_chip_check.js <app.js>'); process.exit(2); }

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
vm.runInContext(src + '\n;globalThis.__x = { state, buildCard };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.buildCard !== 'function') {
  console.error('could not load buildCard from app.js');
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

/* 1. A card that grew the code shows "+412 −38" in one chip. */
const grown = X.buildCard(note({
  session: { diff_stat: { files: 3, added: 412, removed: 38 } },
}));
const chips = grown.querySelectorAll('.chip-diff-stat');
check(chips.length === 1, 'a card with a diff_stat renders exactly one diff-stat chip, got ' + chips.length);
if (chips.length === 1) {
  check(chips[0].textContent === '+412 \u221238',
    'the chip reads "+412 \u221238", got ' + JSON.stringify(chips[0].textContent));
  check(chips[0].title && chips[0].title.indexOf('412') !== -1
        && chips[0].title.indexOf('38') !== -1,
    'the hover title names the added and removed lines, got ' + JSON.stringify(chips[0].title));
  check(chips[0].title && chips[0].title.indexOf('3') !== -1,
    'the hover title names the file count, got ' + JSON.stringify(chips[0].title));
}

/* 2. A card that only removed lines still shows both numbers. */
const shrunk = X.buildCard(note({
  id: 'n2', session: { diff_stat: { files: 1, added: 0, removed: 120 } },
}));
const shrunkChip = shrunk.querySelector('.chip-diff-stat');
check(!!shrunkChip && shrunkChip.textContent === '+0 \u2212120',
  'a deletion-only card reads "+0 \u2212120", got ' + JSON.stringify(shrunkChip && shrunkChip.textContent));

/* 3. No diff_stat (sandbox run, or a card from before this existed): no chip. */
for (const session of [null, {}, { tokens: 42 }, { diff_stat: null }]) {
  const plain = X.buildCard(note({ id: 'n3', session }));
  check(plain.querySelectorAll('.chip-diff-stat').length === 0,
    'no diff_stat -> no diff-stat chip, got one for session ' + JSON.stringify(session));
}

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);
