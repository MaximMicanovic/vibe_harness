/* Red-string direction reproducer for card f577f3.
 *
 * Usage: node tests/string_direction_check.js <path-to-app.js>
 *
 * Spec: "The arrows that set draft cards in que are pointing in the wrong
 * direction. They should point from draft towards the running/reviewing
 * cards."
 *
 * A dependency link is a `depends_on` edge: dragging a draft card onto another
 * card makes the DRAFT the dependent (it runs after the card it was dropped
 * on). The board draws that edge as a red string in `renderStrings()`.
 *
 * This script boots the served app.js under a minimal DOM stub, lays out a
 * draft card and the running/reviewing card it is queued behind at two
 * different, known positions, renders the strings, and reads the drawn path
 * back:
 *
 *   - the path must START at the draft card's anchor (the string is pinned to
 *     the draft), and
 *   - the path must END at the running/reviewing card's anchor, where
 *     `marker-end` puts the arrowhead.
 *
 * On the original code the path runs the other way (pinned to the
 * running/reviewing card, arrowhead on the draft), so this exits 1.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node string_direction_check.js <app.js>'); process.exit(2); }

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

/* `.card[data-card-id="x"]` (what renderStrings uses to find a card) plus the
 * plain `.class` / tag selectors the rest of app.js asks for. */
const ATTR_SEL = /^(?:\.([\w-]+))?([a-zA-Z]*)\[data-card-id="(.*)"\]$/;

function matchSimple(n, sel) {
  if (!n.classList) return false;
  const attr = ATTR_SEL.exec(sel);
  if (attr) {
    if (attr[1] && !n.classList.contains(attr[1])) return false;
    if (attr[2] && n.tagName !== attr[2].toUpperCase()) return false;
    return n.dataset && n.dataset.cardId === attr[3];
  }
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
    textContent: '', value: '', offsetLeft: 0, offsetTop: 0, offsetWidth: 200,
    offsetHeight: 140, clientWidth: 1200, clientHeight: 800,
    scrollHeight: 20, scrollTop: 0, scrollWidth: 0,
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
    node.children = node.children.filter((x) => x !== c);
    node.childNodes = node.childNodes.filter((x) => x !== c);
  };
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
  // The boot refresh() must not wipe the scenario's cards: answer /api/cards
  // with whatever the scenario installed on state.
  fetch: async (path) => ({
    ok: true, status: 200, statusText: 'OK',
    json: async () => {
      const p = String(path);
      if (p.startsWith('/api/cards')) return sandbox.__x && sandbox.__x.state ? sandbox.__x.state.cards : [];
      if (p.startsWith('/api/stacks')) return [];
      if (p.startsWith('/api/projects')) return [];
      return {};
    },
    text: async () => '',
  }),
  requestAnimationFrame: (fn) => fn(),
  getComputedStyle: () => ({ fontSize: '15px' }),
  CSS: { escape: (s) => String(s) },
  navigator: { platform: 'test', userAgentData: null },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  Date, Math, JSON, Promise, Set, Map, Array, Object, String, Number, Boolean,
  parseFloat, parseInt, isNaN, RegExp, Error,
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
vm.runInContext(src + '\n;globalThis.__x = { state, renderStrings };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.renderStrings !== 'function') {
  console.error('could not load renderStrings from app.js');
  process.exit(2);
}

/* ---------- scenario ---------- */

const BOARD_RECT = { left: 0, top: 0, width: 1200, height: 800, right: 1200, bottom: 800 };
/* Two clearly separated positions, so a swapped path is unmistakable. */
const DRAFT_RECT = { left: 100, top: 400, width: 200, height: 140, right: 300, bottom: 540 };
const LIVE_RECT = { left: 700, top: 100, width: 200, height: 140, right: 900, bottom: 240 };

/* cardAnchor(): top-centre of the card, in board-local coords. */
const anchorOf = (r) => ({ x: r.left - BOARD_RECT.left + r.width / 2, y: r.top - BOARD_RECT.top + 6 });

function note(id, status, dependsOn) {
  return {
    id, title: 'note-' + id, body: 'body of ' + id, status,
    created_at: '2026-11-01T10:00:00Z', updated_at: '2026-11-01T10:00:00Z',
    project: '', template: null, auto_run: false, auto_accept: false,
    depends_on: dependsOn || [], session: null, questions: [],
  };
}

const board = getEl('board');
const stringsLayer = getEl('strings-layer');

function resetBoard() {
  board.children = [];
  board.childNodes = [];
  board.getBoundingClientRect = () => BOARD_RECT;
  stringsLayer.textContent = '';
}

/* Lay out one card node on the board at `rect`, the way renderBoard does. */
function placeCard(id, rect) {
  const card = makeEl('article');
  card.classList.add('card');
  card.dataset.cardId = id;
  card.offsetLeft = rect.left;
  card.offsetTop = rect.top;
  card.offsetWidth = rect.width;
  card.offsetHeight = rect.height;
  card.getBoundingClientRect = () => rect;
  board.appendChild(card);
  return card;
}

/* Parse the drawn path: "M x1 y1 Q mx my x2 y2". */
function parsePath(d) {
  const nums = String(d).match(/-?\d+(?:\.\d+)?/g) || [];
  if (nums.length < 6) return null;
  const v = nums.map(Number);
  return { start: { x: v[0], y: v[1] }, end: { x: v[4], y: v[5] } };
}

const failures = [];
const cases = [];

function checkLink(label, draftId, liveId, liveStatus) {
  resetBoard();
  X.state.selectedProject = '';
  X.state.cards = [
    note(draftId, 'draft', [liveId]),   // the queued draft: runs after liveId
    note(liveId, liveStatus, []),       // the card it is queued behind
  ];
  placeCard(draftId, DRAFT_RECT);
  placeCard(liveId, LIVE_RECT);

  X.renderStrings();

  const paths = stringsLayer.children.filter((n) => n.tagName === 'PATH');
  const pins = stringsLayer.children.filter((n) => n.tagName === 'CIRCLE');
  const draftAnchor = anchorOf(DRAFT_RECT);
  const liveAnchor = anchorOf(LIVE_RECT);
  const detail = {
    label, draftId, liveId, liveStatus,
    draftAnchor, liveAnchor,
    pathCount: paths.length,
    pinCount: pins.length,
    path: paths.length ? paths[0].attributes.d : null,
    markerEnd: paths.length ? paths[0].attributes['marker-end'] : null,
    pin: pins.length ? { x: Number(pins[0].attributes.cx), y: Number(pins[0].attributes.cy) } : null,
  };
  cases.push(detail);

  if (paths.length !== 1) {
    failures.push(label + ': expected exactly 1 red string, got ' + paths.length);
    return;
  }
  const parsed = parsePath(detail.path);
  if (!parsed) { failures.push(label + ': could not parse path d=' + detail.path); return; }
  detail.start = parsed.start;
  detail.end = parsed.end;

  const near = (a, b) => Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5;
  if (!near(parsed.start, draftAnchor)) {
    failures.push(label + ': string must START at the draft card ' + draftId
      + ' ' + JSON.stringify(draftAnchor) + ', got ' + JSON.stringify(parsed.start));
  }
  if (!near(parsed.end, liveAnchor)) {
    failures.push(label + ': arrowhead must END on the ' + liveStatus + ' card ' + liveId
      + ' ' + JSON.stringify(liveAnchor) + ', got ' + JSON.stringify(parsed.end));
  }
  if (detail.markerEnd !== 'url(#string-arrow)') {
    failures.push(label + ': the end of the string must carry the arrowhead marker, got ' + detail.markerEnd);
  }
  if (detail.pinCount !== 1) {
    failures.push(label + ': expected exactly 1 pin, got ' + detail.pinCount);
  } else if (!near(detail.pin, draftAnchor)) {
    failures.push(label + ': the pin must sit on the draft card ' + draftId
      + ' ' + JSON.stringify(draftAnchor) + ', got ' + JSON.stringify(detail.pin));
  }
}

/* The spec's two cases: a draft queued behind a running card, and behind a
 * reviewing card. */
checkLink('draft queued behind a running card', 'draft1', 'run1', 'running');
checkLink('draft queued behind a reviewing card', 'draft2', 'rev1', 'reviewing');

const result = { ok: failures.length === 0, failures, cases };
console.log(JSON.stringify(result, null, 2));
process.exit(result.ok ? 0 : 1);
