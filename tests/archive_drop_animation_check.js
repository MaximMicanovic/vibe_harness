/* Archive drop-down animation reproducer for card b270d9.
 *
 * Usage: node tests/archive_drop_animation_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and opens/closes the
 * sidebar archive browser's drop-downs, checking the spec:
 *
 *   "Make a nice animation for the drop down menu on the archive menu."
 *
 * The archive browser is a tree of drop-downs (project header -> archive
 * folder -> project group). Each one folds out under its header instead of the
 * whole panel being rebuilt: the body starts at height 0, grows to its natural
 * height on the next frame (CSS transitions the height), and folds back the
 * same way — the cards leave the DOM at once, the space collapses smoothly and
 * the body is dropped when the transition ends. The chevron rotates instead of
 * swapping glyphs.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node archive_drop_animation_check.js <app.js>'); process.exit(2); }

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
    textContent: '', value: '',
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
  // Reading offsetHeight is a style flush: it is what gives the fold transition
  // its start value. Record when it happens and what the inline height was, so
  // the test can tell the flush happened while the start height was pinned.
  Object.defineProperty(node, 'offsetHeight', {
    configurable: true,
    get() {
      node._offsetReads = (node._offsetReads || 0) + 1;
      node._offsetReadAt = node.style.height;
      return 140;
    },
  });
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

/* ---------- the archive the stub serves ---------- */
const ARCHIVE = {
  id: 'a1', name: 'Project pile', type: 'project', description: '',
  project: 'Alpha',
  created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T12:00:00Z',
  archived_projects: [{ name: 'Beta', archived_at: '2026-10-30T11:00:00Z' }],
  path: '/tmp/archives/project-pile-a1', item_count: 3,
};
function card(id, title, project) {
  return { id, title, project: project || null, status: 'accepted', body: 'b',
    created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z',
    archived: true, archive: 'a1', archived_at: '2026-10-30T12:00:00Z' };
}
const CARDS = {
  alpha1: card('alpha1', 'Alpha one', 'Alpha'),
  alpha2: card('alpha2', 'Alpha two', 'Alpha'),
  loose: card('loose', 'Loose card', null),
};
const FULL = {
  ...ARCHIVE,
  items: [CARDS.alpha1, CARDS.alpha2, CARDS.loose],
  projects: [
    { name: 'Alpha', count: 2, archived_at: null, cards: [CARDS.alpha1, CARDS.alpha2] },
    { name: 'Beta', count: 0, archived_at: '2026-10-30T11:00:00Z', cards: [] },
    { name: null, count: 1, archived_at: null, cards: [CARDS.loose] },
  ],
};

/* Frames and timers are queued, not run: the animation is observable only if
 * the test decides when the next frame / the transition end happens. */
const frames = [];
const timers = [];
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
  fetch: async (path) => {
    const p = String(path);
    let payload = {};
    if (p === '/api/archives') payload = [ARCHIVE];
    else if (p === '/api/archives/a1') payload = FULL;
    else if (p.startsWith('/api/cards')) payload = [];
    else if (p.startsWith('/api/projects')) payload = [];
    else if (p.startsWith('/api/stacks')) payload = [];
    return {
      ok: true, status: 200, statusText: 'OK',
      json: async () => payload,
      text: async () => '',
    };
  },
  requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
  getComputedStyle: () => ({ fontSize: '15px' }),
  CSS: { escape: (s) => String(s) },
  navigator: { platform: 'test', userAgentData: null },
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
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
vm.runInContext(src + '\n;globalThis.__x = { state, renderArchivePanel };',
  sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.renderArchivePanel !== 'function') {
  console.error('could not load renderArchivePanel from app.js');
  process.exit(2);
}

const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
/* Run the queued animation frames (the browser's next paint). */
const runFrames = () => { const due = frames.splice(0); for (const f of due) f(); };
/* Run the queued timers (the transition's safety net). */
const runTimers = () => { const due = timers.splice(0); for (const t of due) if (t) t.fn(); };
/* The browser fires transitionend when the height transition is over. */
const fireTransitionEnd = (node) => {
  for (const fn of (node.listeners.transitionend || []).slice()) fn({ target: node });
};
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
const directChild = (node, cls) => node.children.find((c) => c.classList.contains(cls)) || null;

async function main() {
  await flush();                       // let the boot refresh() land
  runFrames();
  await X.renderArchivePanel();        // the sidebar's archive browser
  await flush();
  runFrames();

  const body = getEl('sidebar-body');

  /* --- level 1: the project header folds out --- */
  const projectHead = body.querySelector('.archive-project-head');
  check(!!projectHead, 'the archive browser renders a project header');
  if (!projectHead) return finish();
  const chevron = projectHead.querySelector('.archive-chevron');
  check(!!chevron, 'the header carries a chevron element (it rotates instead of swapping glyphs)');
  check(!!chevron && !chevron.classList.contains('archive-chevron--open'),
    'the chevron starts folded');

  await projectHead.listeners.click[0]();
  await flush();
  const projectBox = body.querySelector('.archive-project');
  const projectBody = projectBox && directChild(projectBox, 'archive-project-body');
  check(!!projectBody, 'expanding the project header appends its body');
  if (!projectBody) return finish();
  check(projectBody.classList.contains('archive-drop--open'),
    'the opened body carries the drop-open animation class');
  check(projectBody.style.height === '0px',
    'the opened body starts folded (height 0) so the fold-out can animate, got '
    + JSON.stringify(projectBody.style.height));
  check(projectBody._offsetReads > 0 && projectBody._offsetReadAt === '0px',
    'the fold-out forces a style flush while the start height is pinned (height: 0), got '
    + JSON.stringify(projectBody._offsetReadAt));
  check(!!chevron && chevron.classList.contains('archive-chevron--open'),
    'the chevron turns to point down when the menu opens');
  runFrames();
  check(projectBody.style.height === projectBody.scrollHeight + 'px',
    'the next frame grows the body to its natural height, got '
    + JSON.stringify(projectBody.style.height));
  fireTransitionEnd(projectBody);
  check(!projectBody.classList.contains('archive-drop--open'),
    'the animation class is dropped once the fold-out is over');
  check(projectBody.style.height === '',
    'the inline height is cleared after the fold-out so the body can grow with its content');

  /* --- level 2: the archive folder folds out and back --- */
  const folderHead = body.querySelector('.archive-folder-head');
  check(!!folderHead, 'the archive folder renders a clickable header');
  if (!folderHead) return finish();
  await folderHead.listeners.click[0]();
  await flush();
  const folder = body.querySelector('.archive-folder');
  const folderBody = folder && directChild(folder, 'archive-folder-body');
  check(!!folderBody, 'expanding the archive folder appends its body');
  if (!folderBody) return finish();
  check(folderBody.classList.contains('archive-drop--open'),
    'the archive folder body folds out with the same animation');
  runFrames();
  const folderChevron = folderHead.querySelector('.archive-chevron');
  check(!!folderChevron && folderChevron.classList.contains('archive-chevron--open'),
    'the archive folder chevron points down while it is open');

  /* The panel is not rebuilt on a toggle: the same folder node keeps animating. */
  await folderHead.listeners.click[0]();
  await flush();
  check(body.querySelector('.archive-folder') === folder,
    'folding a drop-down animates in place — the panel is not rebuilt');
  check(folderBody.classList.contains('archive-drop--closing'),
    'the closing body carries the fold-away animation class');
  check(folderBody.querySelectorAll('.archive-item').length === 0,
    'the cards leave the DOM at once when the menu folds (the state is already closed)');
  check(folderBody.style.height && folderBody.style.height !== '0px',
    'the closing body pins its current height before folding, got '
    + JSON.stringify(folderBody.style.height));
  check(folderBody._offsetReads > 0 && folderBody._offsetReadAt
    && folderBody._offsetReadAt !== '0px',
    'the fold-away forces a style flush while the start height is pinned, got '
    + JSON.stringify(folderBody._offsetReadAt));
  runFrames();
  check(folderBody.style.height === '0px',
    'the next frame folds the body to height 0, got ' + JSON.stringify(folderBody.style.height));
  check(!!folderChevron && !folderChevron.classList.contains('archive-chevron--open'),
    'the chevron turns back when the menu folds');
  fireTransitionEnd(folderBody);
  check(!directChild(folder, 'archive-folder-body'),
    'the folded body leaves the DOM when the transition ends');

  /* --- and it opens again, with the cards back --- */
  await folderHead.listeners.click[0]();
  await flush();
  const reopened = directChild(folder, 'archive-folder-body');
  check(!!reopened && reopened.classList.contains('archive-drop--open'),
    're-opening the folder folds a fresh body out');
  check(!!reopened && reopened.querySelectorAll('.archive-project').length === 3,
    'the re-opened folder shows its project headers again');
  runFrames();

  /* --- level 3: a project header inside the folder --- */
  const alpha = reopened && reopened.querySelectorAll('.archive-project')
    .find((g) => {
      const n = g.querySelector('.archive-project-name');
      return n && n.textContent === 'Alpha';
    });
  check(!!alpha, 'the folder holds the Alpha project header');
  if (alpha) {
    const alphaHead = alpha.querySelector('.archive-project-head');
    const alphaBody = directChild(alpha, 'archive-project-body');
    check(!!alphaBody && alphaBody.querySelectorAll('.archive-item').length === 2,
      'the project header inside the folder is open by default, with its cards');
    await alphaHead.listeners.click[0]();
    await flush();
    check(alphaBody.classList.contains('archive-drop--closing'),
      'folding the project header animates its cards away');
    check(alphaBody.querySelectorAll('.archive-item').length === 0,
      'the cards under the project header leave the DOM at once');
    runFrames();
    fireTransitionEnd(alphaBody);
    check(!directChild(alpha, 'archive-project-body'),
      'the folded project body leaves the DOM');
    await alphaHead.listeners.click[0]();
    await flush();
    const alphaAgain = directChild(alpha, 'archive-project-body');
    check(!!alphaAgain && alphaAgain.classList.contains('archive-drop--open'),
      're-opening the project header folds its cards out again');
    check(!!alphaAgain && alphaAgain.querySelectorAll('.archive-item').length === 2,
      'the re-opened project header shows its two cards');
    runFrames();
  }

  finish();
}

function finish() {
  const result = { ok: failures.length === 0, failures };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(2);
});
