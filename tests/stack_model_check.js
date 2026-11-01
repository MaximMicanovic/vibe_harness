/* Stack-model reproducer for note 5fe1eb.
 *
 * Usage: node tests/stack_model_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub, stacks several cards the
 * way drag&drop does (stackCards(dragId, targetId)), and checks the spec:
 *
 *   "multiple cards should be able to be stacked and when stacked the card
 *    below should be on top of the stack"
 *
 * Concretely, after dropping cards onto card `a`:
 *   - all dropped cards end up in ONE stack together with `a` (multiple cards
 *     can be stacked), and
 *   - the card below — the drop target `a`, the card the others were stacked
 *     onto — is ON TOP of the stack: last entry of `stack.ids`, highest `--i`,
 *     and therefore the last `.stack-card` DOM child (painted on top, since
 *     stack cards are position:absolute with z-index auto).
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const { makeStacksApi } = require('./stacks_api_stub.js');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node stack_model_check.js <app.js>'); process.exit(2); }

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
    textContent: '', value: '', offsetHeight: 140,
    scrollHeight: 20, clientHeight: 20, scrollTop: 0, scrollWidth: 0, scrollHeight2: 0,
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
    createTextNode: (t) => ({ textContent: t }),
    addEventListener: () => {},
    body: makeEl('body'),
  },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  // Stacks are server state: the stub answers /api/stacks* so the board's
  // piles survive the refresh() that follows every mutation. The notes it
  // validates against are the ones the scenario installs on state.
  fetch: makeStacksApi(() => (sandbox.__x && sandbox.__x.state ? sandbox.__x.state.cards : []),
    async (path) => ({
      ok: true, status: 200, statusText: 'OK',
      json: async () => {
        const p = String(path);
        // The boot refresh() must not wipe the scenario's notes.
        if (p.startsWith('/api/cards')) return sandbox.__x && sandbox.__x.state ? sandbox.__x.state.cards : [];
        if (p.startsWith('/api/projects')) return [];
        return {};
      },
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
// window scroll stub: renderBoard() pins window.scrollX/scrollY across the
// rebuild (note a64cea), so the stub window needs the scroll API.
sandbox.scrollX = 0;
sandbox.scrollY = 0;
sandbox.scrollTo = (x, y) => { sandbox.scrollX = x; sandbox.scrollY = y; };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(APP_JS, 'utf8');
vm.runInContext(src + '\n;globalThis.__x = { state, stackCards, unstackCard, renderBoard, findStackOf, buildStack };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.stackCards !== 'function') {
  console.error('could not load stack functions from app.js');
  process.exit(2);
}

/* ---------- scenario: stack several cards onto card `a` ---------- */
function note(id, updated) {
  return { id, title: 'note-' + id, body: 'body of ' + id, status: 'done', created_at: updated,
    updated_at: updated, project: '', template: null, auto_run: false, auto_accept: false,
    depends_on: [], session: null, questions: [] };
}
X.state.cards = [
  note('a', '2026-10-29T10:00:00Z'),
  note('b', '2026-10-29T11:00:00Z'),
  note('c', '2026-10-29T12:00:00Z'),
  note('d', '2026-10-29T13:00:00Z'),
];
X.state.selectedProject = '';

/* Stack mutations are async now: they go through the server (POST/PATCH
 * /api/stacks) and the board re-reads the piles afterwards. Drain the
 * microtask queue so each drop has landed before the next one. */
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };

async function main() {
// The boot refresh() is async: let it land before the scenario installs its
// own cards, or it overwrites them with the empty boot response.
await flush();
X.renderBoard();
X.stackCards('b', 'a');   // drop b onto a
await flush();
X.stackCards('c', 'a');   // drop c onto the pile's visible card (a)
await flush();
X.stackCards('d', 'a');   // drop d onto the pile's visible card (a)
await flush();

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

const stacks = X.state.stacks;
check(stacks.length === 1, 'expected exactly 1 stack after stacking 3 cards, got ' + stacks.length);
const ids = stacks.length === 1 ? stacks[0].ids : [];
check(ids.length === 4, 'multiple cards should stack into one pile: expected 4 ids in the stack, got ' + JSON.stringify(ids));
check(ids[ids.length - 1] === 'a',
  'the card below (drop target "a") should be ON TOP of the stack (last of ids ' +
  JSON.stringify(ids) + '), got top=' + JSON.stringify(ids[ids.length - 1]));

/* rendered DOM: the top of the stack must be the last .stack-card child
 * (stack cards are position:absolute, z-index auto => DOM order = paint
 * order, so the last child is painted on top) */
X.renderBoard();
const stackEl = getEl('col-done').querySelector('.stack');
check(!!stackEl, 'a .stack element renders in the done column');
if (stackEl) {
  const cards = stackEl.querySelectorAll('.stack-card');
  check(cards.length === 4, 'all 4 stacked cards render inside the stack (got ' + cards.length + ')');
  const domOrder = cards.map((c) => c.dataset.cardId);
  const idx = cards.map((c) => parseInt(c.style.getPropertyValue('--i'), 10));
  check(idx.every((v, i) => v === i), '--i runs 0..n-1 in DOM order, got ' + JSON.stringify(idx));
  check(domOrder[domOrder.length - 1] === 'a',
    'the card below ("a") must be the last .stack-card in DOM order (painted on top); DOM order=' + JSON.stringify(domOrder));
}

/* unstacking must keep working: pull the top card out onto the column */
X.unstackCard('a');
await flush();
const after = X.state.stacks;
check(after.length === 1 && after[0].ids.length === 3 && after[0].ids.indexOf('a') === -1,
  'unstack still removes the card from the pile, got ' + JSON.stringify(after));

const result = { ok: failures.length === 0, failures, ids, domOrder: stackEl ? stackEl.querySelectorAll('.stack-card').map((c) => c.dataset.cardId) : null };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);
}

main();
