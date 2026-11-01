/* Drag-vs-poll reproducer for note 2526b0 ("deck stacking still dosent
 * function").
 *
 * Usage: node tests/stack_drag_poll_repro.cjs <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and drives the REAL
 * drag&drop event flow (dragstart -> poll re-render mid-drag -> dragover/drop
 * on the target card -> dragend), the way a browser does it while the 2 s
 * live-note poll is active.
 *
 * Browser semantics encoded in the stub (the crux of the bug): when the
 * source node of an HTML5 drag is removed from the DOM mid-drag, the browser
 * CANCELS the drag — dragover/drop are no longer delivered anywhere, so the
 * drop never lands. `renderBoard()` tears every column down
 * (`wrap.textContent = ''`), so a poll firing mid-drag removes the dragged
 * card and kills the drag.
 *
 * Scenario: notes a + b in "done", one live "running" note (so the 2 s poll
 * is active, as on the real board). Drag b, let the poll's refresh() run
 * mid-drag, then drop on a. Expected: the drop is delivered and one stack
 * [b, a] forms with a (the card below / drop target) on top.
 *
 * Exits 0 and prints JSON when the drop lands and the stack forms; exits 1
 * with the failures otherwise (e.g. drag cancelled by the mid-drag rebuild).
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const { makeStacksApi } = require('./stacks_api_stub.js');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node stack_drag_poll_repro.cjs <app.js>'); process.exit(2); }

/* ---------- minimal DOM stub (enough for app.js to boot) ---------- */

/* Browser drag state emulated by the stub. */
let dragSource = null;      // element the browser is currently dragging
let dragCancelled = false;  // set when the drag source leaves the DOM mid-drag

function subtreeHas(node, el) {
  if (node === el) return true;
  return (node.children || []).some((c) => subtreeHas(c, el));
}
/* Emulate DOM removal: removing the drag source mid-drag cancels the drag. */
function emulateRemoval(parent, child) {
  if (dragSource && subtreeHas(child, dragSource)) dragCancelled = true;
  child.parentNode = null;
}

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
    value: '', offsetHeight: 140,
    scrollHeight: 20, clientHeight: 20, scrollTop: 0, scrollWidth: 0,
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
  // renderBoard places items with insertBefore so an in-position node is never
  // re-inserted (a re-insert restarts its CSS transitions).
  node.insertBefore = (c, ref) => {
    if (ref === null || ref === undefined) return node.appendChild(c);
    const at = node.children.indexOf(ref);
    if (at === -1) throw new Error('insertBefore: reference node is not a child');
    if (c.parentNode) {
      const p = c.parentNode;
      p.children = p.children.filter((x) => x !== c);
      p.childNodes = p.childNodes.filter((x) => x !== c);
    }
    c.parentNode = node;
    node.children.splice(at, 0, c);
    node.childNodes.splice(at, 0, c);
    return c;
  };
  Object.defineProperty(node, 'firstElementChild', { get() { return node.children[0] || null; } });
  Object.defineProperty(node, 'nextElementSibling', {
    get() {
      if (!node.parentNode) return null;
      const sibs = node.parentNode.children;
      return sibs[sibs.indexOf(node) + 1] || null;
    },
  });
  node.removeChild = (c) => {
    emulateRemoval(node, c);
    node.children = node.children.filter((x) => x !== c);
    node.childNodes = node.childNodes.filter((x) => x !== c);
  };
  node.remove = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  Object.defineProperty(node, 'textContent', {
    get() { return node._text || ''; },
    set(v) {
      node._text = String(v);
      if (v === '') {
        // setting textContent='' removes every descendant (browser semantics)
        for (const c of node.children) emulateRemoval(node, c);
        node.children = []; node.childNodes = [];
      }
    },
  });
  node.setAttribute = (k, v) => { node.attributes[k] = String(v); };
  node.getAttribute = (k) => node.attributes[k];
  node.addEventListener = (type, fn) => { (node.listeners[type] = node.listeners[type] || []).push(fn); };
  node.removeEventListener = () => {};
  node.querySelector = (sel) => node.querySelectorAll(sel)[0] || null;
  node.querySelectorAll = (sel) => {
    const parts = sel.split(/\s+/);
    const out = [];
    const walk = (n) => { for (const c of (n.children || [])) { if (matchComplex(c, parts)) out.push(c); walk(c); } };
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

/* The notes the API serves (a, b done; r running => the 2 s poll is active). */
const NOTES = [
  { id: 'a', title: 'note-a', body: 'body a', status: 'done', created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T10:00:00Z', project: '', template: null, auto_run: false, auto_accept: false, depends_on: [], session: null, questions: [] },
  { id: 'b', title: 'note-b', body: 'body b', status: 'done', created_at: '2026-10-29T11:00:00Z', updated_at: '2026-10-29T11:00:00Z', project: '', template: null, auto_run: false, auto_accept: false, depends_on: [], session: null, questions: [] },
  { id: 'r', title: 'note-r', body: 'body r', status: 'running', created_at: '2026-10-29T12:00:00Z', updated_at: '2026-10-29T12:00:00Z', project: '', template: null, auto_run: false, auto_accept: false, depends_on: [], session: { started_at: '2026-10-29T12:00:00Z', live_tokens: 10 }, questions: [] },
];
const sandbox = {
  console,
  document: {
    getElementById: getEl,
    createElement: (t) => makeEl(t),
    createElementNS: (ns, t) => makeEl(t),
    createTextNode: (t) => { const n = makeEl('#text'); n._text = String(t); return n; },
    addEventListener: () => {},
    querySelectorAll: () => [],
    body: makeEl('body'),
  },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  // Stacks are server state: the stub answers /api/stacks* so the board's
  // piles survive the refresh() that follows every mutation.
  fetch: makeStacksApi(() => NOTES, async (path) => ({
    ok: true, status: 200, statusText: 'OK',
    json: async () => (String(path).startsWith('/api/cards') ? NOTES : {}),
    text: async () => '',
  })),
  requestAnimationFrame: (fn) => fn(),
  getComputedStyle: () => ({ fontSize: '15px' }),
  CSS: { escape: (s) => String(s) },
  navigator: { platform: 'test', userAgentData: null },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  Date, Math, JSON, Promise, Set, Map, Array, Object, String, Number, Boolean, parseFloat, parseInt, isNaN, RegExp, Error,
};
sandbox.addEventListener = () => {};
sandbox.confirm = () => true;
sandbox.scrollX = 0;
sandbox.scrollY = 0;
sandbox.scrollTo = (x, y) => { sandbox.scrollX = x; sandbox.scrollY = y; };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(APP_JS, 'utf8');
vm.runInContext(src + '\n;globalThis.__x = { state, stackCards, unstackCard, renderBoard, refresh, findStackOf };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.refresh !== 'function') {
  console.error('could not load app.js functions');
  process.exit(2);
}

/* ---------- event dispatch with browser drag semantics ---------- */
const dataTransfer = {
  _d: {}, effectAllowed: '', dropEffect: '',
  setData(t, v) { this._d[t] = String(v); },
  getData(t) { return this._d[t] || ''; },
};
function fire(node, type) {
  if (type === 'dragstart') { dragSource = node; dragCancelled = false; }
  // After the drag source left the DOM the browser cancelled the drag:
  // dragover/drop are no longer delivered to any element.
  if ((type === 'dragover' || type === 'drop') && dragCancelled) return false;
  const ev = {
    type, target: node, dataTransfer,
    preventDefault() {}, stopPropagation() {},
  };
  (node.listeners[type] || []).slice().forEach((fn) => fn(ev));
  // the drag operation ends at drop (dragend follows immediately after)
  if (type === 'drop' || type === 'dragend') { dragSource = null; }
  return true;
}

/* ---------- scenario: drag b, poll fires mid-drag, drop on a ---------- */
(async () => {
  X.state.cards = NOTES.map((n) => Object.assign({}, n));
  X.state.selectedProject = '';
  X.renderBoard();

  // the stub's selector engine only handles simple class selectors, so find
  // cards by walking the column and matching dataset.cardId
  const cardOf = (id) => {
    const walk = (n) => {
      for (const c of n.children) {
        if (c.classList && c.classList.contains('card') && c.dataset.cardId === id) return c;
        const r = walk(c);
        if (r) return r;
      }
      return null;
    };
    return walk(getEl('col-done'));
  };
  const cardB = cardOf('b');
  const cardA = cardOf('a');
  if (!cardB || !cardA) {
    console.log(JSON.stringify({ ok: false, failures: ['cards a/b did not render in the done column'] }));
    process.exit(1);
  }

  fire(cardB, 'dragstart');                 // user starts dragging card b
  await X.refresh();                        // the 2 s live poll fires MID-DRAG (refresh -> renderBoard)
  const dropDelivered = fire(cardA, 'dragover') && fire(cardA, 'drop');  // user drops b onto a
  fire(cardB, 'dragend');                   // drag ends (fires even when cancelled)

  const failures = [];
  function check(cond, msg) { if (!cond) failures.push(msg); }

  check(dropDelivered, 'the drop on card "a" was delivered (drag not cancelled by the mid-drag poll re-render)');
  const stacks = X.state.stacks;
  check(stacks.length === 1, 'dropping b onto a formed a stack, got ' + JSON.stringify(stacks));
  const ids = stacks.length === 1 ? stacks[0].ids : [];
  check(ids.length === 2 && ids.indexOf('a') !== -1 && ids.indexOf('b') !== -1,
    'the stack holds both cards, got ' + JSON.stringify(ids));
  check(ids[ids.length - 1] === 'a',
    'the card below (drop target "a") is on top of the stack, got ids=' + JSON.stringify(ids));

  // Board must still render after the drag ends (pending rebuild flushed).
  X.renderBoard();
  check(!!cardOf('a') && !!cardOf('b'), 'both cards still render after the drag ended');

  const result = {
    ok: failures.length === 0,
    failures,
    dropDelivered,
    dragCancelled,
    stacks: stacks.map((s) => s.ids),
  };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
