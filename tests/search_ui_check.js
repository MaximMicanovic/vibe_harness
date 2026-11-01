/* Search reproducer: find a card by text, on the board and in the archives.
 *
 * Usage: node tests/search_ui_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   - typing filters the board (title, body, id; case-insensitive) and shows
 *     an "N of M cards" hint while a query is active;
 *   - the results list under the input groups hits by location (Board first,
 *     then each archive) and names the archive an archived card lives in;
 *   - a query matching nothing shows an explicit empty state;
 *   - "/" focuses the search input, Esc clears it and the board comes back;
 *   - the project filter and the search apply together;
 *   - clicking a board result opens that card's panel, clicking an archived
 *     result opens the archive folder holding it and highlights the card.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node search_ui_check.js <app.js>'); process.exit(2); }

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
  // One compound selector: tag, .classes and [attr="value"] parts.
  const attrRe = /\[([\w-]+)="([^"]*)"\]/g;
  let m;
  while ((m = attrRe.exec(sel)) !== null) {
    const key = m[1].replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (n.dataset[key] !== m[2]) return false;
  }
  const bare = sel.replace(attrRe, '');
  const tagPart = bare.split('.')[0];
  if (tagPart && tagPart !== '*' && n.tagName !== tagPart.toUpperCase()) return false;
  const classes = bare.split('.').slice(1).filter(Boolean);
  return classes.every((c) => n.classList.contains(c));
}
/* Split a selector on whitespace, but not inside an attribute value
 * ("[data-where=\"Vibe Harness / Old work\"]" is one part). */
function splitSelector(sel) {
  const parts = [];
  let cur = '';
  let quote = null;
  for (const ch of sel) {
    if (quote) { cur += ch; if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
    if (/\s/.test(ch)) { if (cur) { parts.push(cur); cur = ''; } continue; }
    cur += ch;
  }
  if (cur) parts.push(cur);
  return parts;
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
    scrollHeight: 20, clientHeight: 20, scrollTop: 0, scrollWidth: 0,
    options: [],
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
    const parts = splitSelector(sel);
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (matchComplex(c, parts)) out.push(c); walk(c); } };
    walk(node);
    return out;
  };
  node.closest = (sel) => { let n = node; while (n) { if (n.classList && matchSimple(n, sel)) return n; n = n.parentNode; } return null; };
  node.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 140, right: 200, bottom: 140 });
  node.focus = () => { node._focused = true; };
  node.blur = () => { node._focused = false; };
  node.scrollIntoView = () => { node._scrolled = true; };
  return node;
}

const byId = {};
const roots = [];
const getEl = (id) => {
  if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; roots.push(byId[id]); }
  return byId[id];
};
const store = {};

/* The elements index.html ships with the `hidden` attribute — the stub has no
 * markup, so mirror them here (an overlay that starts "visible" would swallow
 * the keyboard shortcuts). */
for (const id of ['search-hint', 'search-results', 'error-banner', 'sidebar-panel',
                  'empty-board', 'editor-overlay', 'log-overlay', 'archives-overlay',
                  'projects-overlay', 'depends-field', 'accept-hint', 'btn-stop',
                  'btn-accept', 'btn-archive', 'btn-log']) {
  getEl(id).hidden = true;
}

/* ---------- the board + archive the stub serves ---------- */
function card(id, title, body, project, status) {
  return {
    id, title, body, project: project || null, status: status || 'draft',
    created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z',
    depends_on: [], session: null, archived: false, archive: null, archived_at: null,
  };
}
const BOARD = [
  card('b1', 'Alpha board card', 'the alpha body', 'Alpha'),
  card('b2', 'Beta board card', 'the beta body', 'Beta'),
  card('b3', 'Gamma board card', 'nothing relevant here', 'Alpha'),
];
const ARCHIVE = {
  id: 'a1', name: 'Old work', type: 'cards', description: '',
  project: 'Vibe Harness',
  created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T12:00:00Z',
  archived_projects: [], archived_stacks: [], path: '/tmp/archives/old-work-a1',
  item_count: 1,
};
const ARCHIVED = [
  Object.assign(card('x1', 'Retired alpha card', 'an archived alpha body', 'Vibe Harness', 'accepted'),
    { archived: true, archive: 'a1', archived_at: '2026-10-30T12:00:00Z' }),
];
const ARCHIVE_FULL = Object.assign({}, ARCHIVE, {
  items: ARCHIVED,
  projects: [{ name: 'Vibe Harness', count: 1, archived_at: null, cards: ARCHIVED }],
});

/* The server's answer to GET /api/search: a linear scan over the board and
 * every archive, exactly like app/search.py. */
function stubSearch(q) {
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return [];
  const hits = [];
  const scan = (cards, where) => {
    for (const c of cards) {
      const hay = (c.title + '\n' + c.body + '\n' + c.id).toLowerCase();
      if (hay.indexOf(needle) === -1) continue;
      const at = (c.body || '').toLowerCase().indexOf(needle);
      hits.push({
        id: c.id, title: c.title, status: c.status, where,
        snippet: at === -1
          ? { text: c.title, start: 0, end: 0 }
          : { text: c.body, start: at, end: at + needle.length },
      });
    }
  };
  scan(BOARD, 'board');
  scan(ARCHIVED, 'Vibe Harness / Old work');
  return hits;
}

const timers = [];
const docListeners = {};
const sandbox = {
  console,
  document: {
    getElementById: getEl,
    createElement: (t) => makeEl(t),
    createElementNS: (ns, t) => makeEl(t),
    createTextNode: (t) => ({ textContent: t, children: [], childNodes: [], parentNode: null }),
    addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
    querySelector: (sel) => sandbox.document.querySelectorAll(sel)[0] || null,
    querySelectorAll: (sel) => {
      const out = [];
      for (const root of roots) {
        if (matchComplex(root, splitSelector(sel))) out.push(root);
        for (const n of root.querySelectorAll(sel)) if (out.indexOf(n) === -1) out.push(n);
      }
      return out;
    },
    body: makeEl('body'),
  },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  fetch: async (path) => {
    const p = String(path);
    let payload = {};
    let headers = {};
    if (p.startsWith('/api/search')) {
      const q = decodeURIComponent((p.split('q=')[1] || ''));
      payload = stubSearch(q);
      headers = { 'X-Search-Truncated': 'false', 'X-Search-Total': String(payload.length) };
    } else if (p === '/api/cards') payload = BOARD;
    else if (p.startsWith('/api/cards/')) {
      const id = decodeURIComponent(p.slice('/api/cards/'.length));
      payload = BOARD.concat(ARCHIVED).find((c) => c.id === id) || {};
    } else if (p === '/api/archives') payload = [ARCHIVE];
    else if (p === '/api/archives/a1') payload = ARCHIVE_FULL;
    else if (p.startsWith('/api/projects')) payload = [];
    else if (p.startsWith('/api/stacks')) payload = [];
    else if (p.startsWith('/api/models')) payload = { roles: {}, models: [], default_model: null };
    else if (p.startsWith('/api/settings')) payload = { backend: 'vibe', backends: [] };
    else if (p.startsWith('/api/version')) payload = { version: 'test' };
    else if (p.startsWith('/api/stats')) payload = { total_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0 };
    return {
      ok: true, status: 200, statusText: 'OK',
      headers: { get: (k) => (k in headers ? headers[k] : null) },
      json: async () => payload,
      text: async () => '',
    };
  },
  requestAnimationFrame: (fn) => fn(),
  getComputedStyle: () => ({ fontSize: '15px' }),
  CSS: { escape: (s) => String(s) },
  navigator: { platform: 'test', userAgentData: null },
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: (id) => { if (id) timers[id - 1] = null; },
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
vm.runInContext(src + '\n;globalThis.__x = { state, setSearchQuery, clearSearch, renderBoard,'
  + ' visibleCards, cardMatchesQuery, searchNeedle, openSearchResult, renderSearchResults };',
  sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.setSearchQuery !== 'function') {
  console.error('could not load setSearchQuery from app.js');
  process.exit(2);
}

const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); };
const runTimers = () => { const due = timers.splice(0); for (const fn of due) if (fn) fn(); };
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
const texts = (nodes) => nodes.map((n) => n.textContent);

/* The card ids the board currently renders, in column order. */
function boardCardIds() {
  const ids = [];
  for (const col of ['draft', 'running', 'reviewing', 'done', 'accepted', 'failed']) {
    for (const n of getEl('col-' + col).querySelectorAll('.card')) ids.push(n.dataset.cardId);
  }
  return ids.sort();
}

function dispatchKey(key) {
  const event = {
    key, target: { tagName: 'BODY' }, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  for (const fn of docListeners.keydown || []) fn(event);
  return event;
}

async function main() {
  await flush();                       // let the boot refresh() land
  const resultsEl = getEl('search-results');
  const hint = getEl('search-hint');
  const input = getEl('search-input');

  check(boardCardIds().join(',') === 'b1,b2,b3',
    'the board starts with every card, got ' + boardCardIds().join(','));
  check(hint.hidden === true, 'no "N of M cards" hint before a query');

  /* --- 1. typing filters the board and shows the hint --- */
  X.setSearchQuery('alpha');
  await flush();
  check(boardCardIds().join(',') === 'b1',
    'typing filters the board to the matching cards, got ' + boardCardIds().join(','));
  check(hint.hidden === false, 'the "N of M cards" hint shows while a query is active');
  check(hint.textContent === '1 of 3 cards',
    'the hint counts matching of total cards, got ' + JSON.stringify(hint.textContent));

  /* --- 2. the results list groups hits by location --- */
  runTimers();
  await flush();
  const groups = resultsEl.querySelectorAll('.search-group');
  check(groups.length === 2, 'the results list groups hits by location, got ' + groups.length + ' groups');
  const heads = texts(groups.map((g) => g.querySelector('.search-group-head')));
  check(JSON.stringify(heads) === JSON.stringify(['Board', 'Vibe Harness / Old work']),
    'Board comes first, then the archive the hit lives in; got ' + JSON.stringify(heads));
  const archivedRow = resultsEl.querySelector('.search-result[data-where="Vibe Harness / Old work"]');
  check(!!archivedRow, 'the archived card is listed under its archive');
  if (archivedRow) {
    const title = archivedRow.querySelector('.search-result-title');
    check(title && title.textContent === 'Retired alpha card',
      'the archived hit names the card, got ' + (title ? title.textContent : null));
    const mark = archivedRow.querySelector('.search-hit');
    check(mark && mark.textContent.toLowerCase() === 'alpha',
      'the snippet highlights the match, got ' + (mark ? mark.textContent : null));
  }

  /* --- 3. a query matching nothing shows an explicit empty state --- */
  X.setSearchQuery('zzz-nothing');
  await flush();
  runTimers();
  await flush();
  check(boardCardIds().length === 0, 'a query matching nothing hides every card');
  const empty = resultsEl.querySelector('.search-empty');
  check(!!empty, 'a query matching nothing shows an explicit empty state, not a blank panel');
  check(empty && empty.textContent.indexOf('No cards match') !== -1,
    'the empty state says nothing matched, got ' + (empty ? empty.textContent : null));
  check(resultsEl.hidden === false, 'the results panel stays visible for the empty state');
  const emptyBoard = getEl('empty-board');
  check(emptyBoard.hidden === false && emptyBoard.textContent.indexOf('match') !== -1,
    'the board says nothing matched instead of claiming it is empty, got '
    + JSON.stringify(emptyBoard.textContent));

  /* --- 4. "/" focuses the search input --- */
  X.clearSearch();
  await flush();
  input._focused = false;
  const slash = dispatchKey('/');
  check(input._focused === true, '"/" focuses the search input');
  check(slash.defaultPrevented === true, '"/" does not type a slash into the page');

  /* --- 5. Esc clears the query and the board comes back --- */
  X.setSearchQuery('alpha');
  await flush();
  check(boardCardIds().join(',') === 'b1', 'the board is filtered before Esc');
  const esc = dispatchKey('Escape');
  await flush();
  check(X.state.searchQuery === '', 'Esc clears the query');
  check(input.value === '', 'Esc empties the search input');
  check(boardCardIds().join(',') === 'b1,b2,b3',
    'the board returns to normal after Esc, got ' + boardCardIds().join(','));
  check(hint.hidden === true, 'the hint disappears once the query is cleared');
  check(resultsEl.hidden === true, 'the results panel closes once the query is cleared');
  check(esc.defaultPrevented === true, 'Esc on a live query is handled by the search, not an overlay');

  /* --- 6. the project filter and the search apply together --- */
  X.state.selectedProject = 'Alpha';
  X.setSearchQuery('board');
  await flush();
  check(boardCardIds().join(',') === 'b1,b3',
    'the project filter and the search both apply, got ' + boardCardIds().join(','));
  check(hint.textContent === '2 of 2 cards',
    'the hint counts within the project filter, got ' + JSON.stringify(hint.textContent));
  X.state.selectedProject = '';
  X.clearSearch();
  await flush();

  /* --- 7. clicking a board result opens that card's panel --- */
  X.setSearchQuery('alpha');
  await flush();
  runTimers();
  await flush();
  const boardRow = resultsEl.querySelector('.search-result[data-where="board"]');
  check(!!boardRow, 'the board hit is listed under Board');
  if (boardRow) {
    boardRow.listeners.click[0]();
    await flush();
    check(getEl('editor-overlay').hidden === false,
      'clicking a board result opens the card panel');
    check(getEl('card-title').value === 'Alpha board card',
      'the panel shows the card that was clicked, got ' + JSON.stringify(getEl('card-title').value));
  }

  /* --- 8. clicking an archived result opens its folder and highlights it --- */
  const archRow = resultsEl.querySelector('.search-result[data-where="Vibe Harness / Old work"]');
  check(!!archRow, 'the archived hit is listed');
  if (archRow) {
    archRow.listeners.click[0]();
    await flush();
    check(getEl('sidebar-panel').hidden === false,
      'clicking an archived result opens the archive browser');
    check(X.state.sidebarArchiveOpen.has('a1'),
      'the archive folder holding the card is expanded');
    const hitRow = getEl('sidebar-body').querySelector('.archive-item--hit');
    check(!!hitRow, 'the archived card is highlighted in the folder');
    check(hitRow && hitRow.dataset.cardId === 'x1',
      'the highlighted row is the card that was clicked, got '
      + (hitRow ? hitRow.dataset.cardId : null));
  }

  return finish();
}

function finish() {
  if (failures.length) {
    console.log(JSON.stringify({ ok: false, failures }));
    process.exit(1);
  }
  console.log(JSON.stringify({ ok: true, failures: [] }));
  process.exit(0);
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, failures: ['threw: ' + (err && err.stack ? err.stack : err)] }));
  process.exit(1);
});
