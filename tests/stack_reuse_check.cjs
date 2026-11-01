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
  // Emulated layout: a card's height depends on its title length (long titles
  // wrap to two lines -> taller card) and on whether the title is expanded.
  Object.defineProperty(node, 'offsetHeight', {
    get() {
      if (!node.classList || !node.classList.contains('card')) return 20;
      const t = node.querySelector('.card-title');
      const len = t && t._text ? String(t._text).length : 0;
      let h = 140 + (len > 30 ? 60 : 0);
      if (t && t.classList.contains('card-title--expanded')) h += 120;
      return h;
    },
  });
  return node;
}

const byId = {};
const getEl = (id) => { if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; } return byId[id]; };
const store = {};

/* The notes the API serves (a, b done; r running => the 2 s poll is active). */
let CURRENT_NOTES = null;
const NOTES = [
  { id: 'a', title: 'note-a', body: 'body a', status: 'done', created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T10:00:00Z', project: '', template: null, auto_run: false, auto_accept: false, depends_on: [], session: null, questions: [] },
  { id: 'b', title: 'note-b', body: 'body b', status: 'done', created_at: '2026-10-29T11:00:00Z', updated_at: '2026-10-29T11:00:00Z', project: '', template: null, auto_run: false, auto_accept: false, depends_on: [], session: null, questions: [] },
  { id: 'r', title: 'note-r', body: 'body r', status: 'running', created_at: '2026-10-29T12:00:00Z', updated_at: '2026-10-29T12:00:00Z', project: '', template: null, auto_run: false, auto_accept: false, depends_on: [], session: { started_at: '2026-10-29T12:00:00Z', live_tokens: 10 }, questions: [] },
];
/* Stacks are server state: the stub answers /api/stacks* so the board's piles
 * survive the refresh() that follows every mutation. */
const stacksApi = makeStacksApi(() => CURRENT_NOTES, async (path) => ({
  ok: true, status: 200, statusText: 'OK',
  json: async () => (String(path).startsWith('/api/cards') ? CURRENT_NOTES : (String(path).startsWith('/api/projects') ? [] : {})),
  text: async () => '',
}));

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
  fetch: stacksApi,
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
/* Stack mutations are async (server round-trip + board re-read): drain the
 * microtask queue so each one has landed before the next assertion. */
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
(async () => {
  // Graphical-bug repro for stacked cards (overlap + shake):
  //  (a) the 2 s poll must REUSE unchanged card/stack DOM nodes instead of
  //      tearing the column down (node recreation snaps in-flight transitions
  //      and makes fanned stacks jump/shake);
  //  (b) expanding a stacked card's title ("more ▾") must re-measure the
  //      stack box height (--card-h) immediately, else the absolutely
  //      positioned card overflows the stack and overlaps the next item.
  const mk = (id, title, status, upd) => ({
    id, title, body: 'body of ' + id, status, created_at: upd, updated_at: upd,
    project: '', template: null, auto_run: false, auto_accept: false, depends_on: [],
    session: status === 'running' ? { started_at: upd, live_tokens: 5 } : (status === 'accepted' ? { tokens: 100 } : null),
    questions: [],
  });
  const SCENARIO_NOTES = [
    mk('a', 'short a', 'accepted', '2026-10-29T10:00:00Z'),
    mk('b', 'a much longer card title that wraps over two lines', 'accepted', '2026-10-29T10:05:00Z'),
    mk('c', 'mid c', 'accepted', '2026-10-29T10:10:00Z'),
    mk('d', 'plain card d', 'accepted', '2026-10-29T10:12:00Z'),
    mk('r', 'running note r', 'running', '2026-10-29T10:15:00Z'),
  ];
  CURRENT_NOTES = SCENARIO_NOTES.map((n) => Object.assign({}, n));
  X.state.cards = CURRENT_NOTES.map((n) => Object.assign({}, n));
  X.state.selectedProject = '';
  // Piles live on the server now: seed the stub, then let the board read them.
  stacksApi.seed([{ id: 'st-1', col: 'accepted', title: '', ids: ['a', 'b', 'c'] }]);
  await X.refresh();

  const col = getEl('col-accepted');
  // The column item is the wrapper (.stack-wrap: header + pile); the pile
  // inside it (.stack) is what carries the fan layout.
  const findStack = () => col.children.find((c) => c.classList && c.classList.contains('stack-wrap')) || null;
  const findPile = () => { const w = findStack(); return w ? w.querySelector('.stack') : null; };
  const findCard = (id) => {
    let f = null;
    const walk = (n) => { for (const c of n.children) { if (c.classList && c.classList.contains('card') && c.dataset.cardId === id) f = c; walk(c); } };
    walk(col);
    return f;
  };
  const failures = [];
  const check = (cond, msg) => { if (!cond) failures.push(msg); };

  const s1 = findStack();
  const d1 = findCard('d');
  await X.refresh();  // poll, nothing changed
  check(findStack() === s1, 'unchanged poll must reuse the stack DOM node (no teardown)');
  check(findCard('d') === d1, 'unchanged poll must reuse unchanged card DOM nodes');

  CURRENT_NOTES.find((n) => n.id === 'r').session.live_tokens = 99;  // live tick elsewhere
  X.state.cards = CURRENT_NOTES.map((n) => Object.assign({}, n));
  await X.refresh();
  check(findStack() === s1, 'a live tick in another column must not rebuild the stack node');

  CURRENT_NOTES.find((n) => n.id === 'b').updated_at = '2026-10-29T10:05:30Z';  // stack member changed
  X.state.cards = CURRENT_NOTES.map((n) => Object.assign({}, n));
  await X.refresh();
  const s2 = findStack();
  check(s2 !== s1, 'a changed stack member must rebuild the stack node');

  // --- collapsed deck fans its cards out like a hand of cards ---
  const fanAngles = s2.querySelectorAll('.stack-card').map((c) => parseFloat(c.style._props['--fan'] || '0'));
  check(fanAngles.length === 3 && new Set(fanAngles).size === 3,
    'a collapsed deck must tilt each card differently (got ' + JSON.stringify(fanAngles) + ')');
  check(Math.abs(fanAngles.reduce((a, b) => a + b, 0)) < 0.01,
    'the fan must be centred on the middle card (got ' + JSON.stringify(fanAngles) + ')');

  const cardB = s2.querySelectorAll('.stack-card').find((c) => c.dataset.cardId === 'b');
  const toggle = cardB.querySelector('.card-title-toggle');
  fire(toggle, 'click');  // expand the title: the card grows
  const H = parseFloat(findPile().style._props['--card-h'] || '140');
  check(H >= cardB.offsetHeight,
    'expanding a stacked card title must re-measure --card-h immediately (got ' + H + ', card is ' + cardB.offsetHeight + ')');

  // --- unstacking must be obvious: a button on the stack and one per card ---
  fire(s2, 'mouseenter');  // fan the stack out the way hover does
  const unstackAllBtn = s2.querySelector('.stack-unstack');
  check(!!unstackAllBtn, 'a fanned stack must offer an unstack-all button');
  const perCard = s2.querySelectorAll('.card-unstack');
  check(perCard.length === 3,
    'every stacked card must offer its own unstack button (got ' + perCard.length + ')');

  // one card leaves the stack, the rest stays stacked
  const leaving = s2.querySelectorAll('.stack-card')[0];
  const leavingId = leaving.dataset.cardId;
  const leaveBtn = leaving.querySelector('.card-unstack');
  if (leaveBtn) fire(leaveBtn, 'click');
  await flush();  // the unstack goes through the server, then the board re-reads
  const s3 = findStack();
  const idsLeft = s3 ? s3.querySelectorAll('.stack-card').map((c) => c.dataset.cardId) : [];
  check(idsLeft.indexOf(leavingId) === -1,
    'the per-card unstack button must remove that card from the stack');
  check(idsLeft.length === 2,
    'the rest of the stack must stay stacked (got ' + idsLeft.length + ')');
  check(!!findCard(leavingId),
    'the unstacked card must still render as a plain card in the column');

  // the stack button dissolves what is left
  const allBtn = s3 && s3.querySelector('.stack-unstack');
  if (allBtn) { fire(s3, 'mouseenter'); fire(allBtn, 'click'); }
  await flush();
  check(!findStack(), 'the stack unstack-all button must dissolve the stack');
  check(X.state.stacks.length === 0, 'unstacking must be persisted in state.stacks');

  console.log(JSON.stringify({ ok: failures.length === 0, failures }, null, 2));
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
