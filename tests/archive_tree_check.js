/* Archive-tree reproducer for card f0d199.
 *
 * Usage: node tests/archive_tree_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub, opens an archive in the
 * sidebar's archive browser, and checks the spec:
 *
 *   "Make one more level to the drop down menu in the archive so we can have a
 *    project header. Let the archives be project related."
 *
 * Concretely, inside an expanded archive folder the cards are no longer a flat
 * list: there is one more level — a collapsible PROJECT HEADER per project
 * (the card's `project`), with that project's cards nested under it. Cards
 * without a project sit under a "No project" header, and a deck archived
 * wholesale keeps its header even when it holds no cards.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node archive_tree_check.js <app.js>'); process.exit(2); }

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
  // The archive belongs to a project: that is the browser's top level.
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
/* The server groups the archive's cards under project headers (GET
 * /api/archives/<id>): one group per project, unfiled cards last (name null),
 * and a deck archived wholesale keeps its header even with no cards left. */
const FULL = {
  ...ARCHIVE,
  items: [CARDS.alpha1, CARDS.alpha2, CARDS.loose],
  projects: [
    { name: 'Alpha', count: 2, archived_at: null, cards: [CARDS.alpha1, CARDS.alpha2] },
    { name: 'Beta', count: 0, archived_at: '2026-10-30T11:00:00Z', cards: [] },
    { name: null, count: 1, archived_at: null, cards: [CARDS.loose] },
  ],
};

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
vm.runInContext(src + '\n;globalThis.__x = { state, renderArchivePanel, buildArchiveFolder, renderArchiveList };',
  sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.renderArchivePanel !== 'function') {
  console.error('could not load renderArchivePanel from app.js');
  process.exit(2);
}

const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
const texts = (nodes) => nodes.map((n) => n.textContent);

async function main() {
  await flush();                       // let the boot refresh() land
  await X.renderArchivePanel();        // the sidebar's archive browser
  await flush();

  // Level 1: the project header the archive is filed under. It starts folded.
  const projectHead = getEl('sidebar-body').querySelector('.archive-project-head');
  check(!!projectHead, 'the archive browser renders a project header');
  if (!projectHead) return finish();
  const projectName = projectHead.querySelector('.archive-project-name');
  check(projectName && projectName.textContent === 'Alpha',
    'the project header is named after the archive\'s project, got '
    + (projectName ? projectName.textContent : null));
  await projectHead.listeners.click[0]();
  await flush();

  // Level 2: the user expands the archive folder (this fetches its cards).
  const folderHead = getEl('sidebar-body').querySelector('.archive-folder-head');
  check(!!folderHead, 'the archive folder renders a clickable header');
  if (!folderHead) return finish();
  await folderHead.listeners.click[0]();
  await flush();

  const body = getEl('sidebar-body');
  const folders = body.querySelectorAll('.archive-folder');
  check(folders.length === 1, 'one archive folder renders, got ' + folders.length);
  const folder = folders[0];
  if (!folder) return finish();

  const folderBody = folder.querySelector('.archive-folder-body');
  check(!!folderBody, 'the expanded archive folder has a body');
  if (!folderBody) return finish();

  /* --- the new level: a project header per project --- */
  const groups = folderBody.querySelectorAll('.archive-project');
  check(groups.length === 3,
    'the archive body holds one project header per project (Alpha, Beta, No project), got ' + groups.length);
  const names = groups.map((g) => {
    const head = g.querySelector('.archive-project-head');
    const nameEl = head && head.querySelector('.archive-project-name');
    return nameEl ? nameEl.textContent : null;
  });
  check(JSON.stringify(names) === JSON.stringify(['Alpha', 'Beta', 'No project']),
    'project headers are named after their project, unfiled cards last under "No project"; got ' + JSON.stringify(names));

  /* --- the cards are nested UNDER their project header (one more level) --- */
  const directRows = folderBody.children.filter((c) => c.classList.contains('archive-item'));
  check(directRows.length === 0,
    'cards no longer sit flat in the archive body — they are nested under a project header, got '
    + directRows.length + ' flat rows');

  const byName = {};
  for (const g of groups) {
    const head = g.querySelector('.archive-project-head');
    const nameEl = head && head.querySelector('.archive-project-name');
    byName[nameEl ? nameEl.textContent : '?'] = g;
  }
  const alphaRows = byName['Alpha'] ? byName['Alpha'].querySelectorAll('.archive-item') : [];
  check(JSON.stringify(texts(alphaRows.map((r) => r.querySelector('.archive-item-title'))))
    === JSON.stringify(['Alpha one', 'Alpha two']),
    'Alpha\'s cards render under the Alpha header, got '
    + JSON.stringify(texts(alphaRows.map((r) => r.querySelector('.archive-item-title')))));
  const looseRows = byName['No project'] ? byName['No project'].querySelectorAll('.archive-item') : [];
  check(JSON.stringify(texts(looseRows.map((r) => r.querySelector('.archive-item-title'))))
    === JSON.stringify(['Loose card']),
    'the card without a project renders under "No project", got '
    + JSON.stringify(texts(looseRows.map((r) => r.querySelector('.archive-item-title')))));

  /* --- the header is a dropdown level: it collapses and expands --- */
  const alphaHead = byName['Alpha'] && byName['Alpha'].querySelector('.archive-project-head');
  check(!!alphaHead && alphaHead.tagName === 'BUTTON',
    'the project header is a clickable button (a dropdown level)');
  if (alphaHead) {
    await alphaHead.listeners.click[0]();
    await flush();
    // Scoped to the folder body: the browser's top level is also a project
    // header, and it may carry the same name.
    const folderNow = getEl('sidebar-body').querySelector('.archive-folder');
    const collapsed = folderNow ? folderNow.querySelectorAll('.archive-project') : [];
    const alphaCollapsed = collapsed.find((g) => {
      const n = g.querySelector('.archive-project-name');
      return n && n.textContent === 'Alpha';
    });
    check(!!alphaCollapsed, 'the Alpha header survives collapsing');
    check(alphaCollapsed && alphaCollapsed.querySelectorAll('.archive-item').length === 0,
      'clicking the project header collapses its cards (the dropdown closes)');
    const head2 = alphaCollapsed && alphaCollapsed.querySelector('.archive-project-head');
    if (head2) {
      await head2.listeners.click[0]();
      await flush();
      const folderAgain = getEl('sidebar-body').querySelector('.archive-folder');
      const reopened = (folderAgain ? folderAgain.querySelectorAll('.archive-project') : [])
        .find((g) => {
          const n = g.querySelector('.archive-project-name');
          return n && n.textContent === 'Alpha';
        });
      check(reopened && reopened.querySelectorAll('.archive-item').length === 2,
        'clicking it again expands the project\'s cards');
    }
  }

  /* --- the archive folder itself still works --- */
  const headAfter = folder.querySelector('.archive-folder-head');
  check(!!headAfter && headAfter.querySelector('.archive-name').textContent === 'Project pile',
    'the archive folder header still shows the archive name');

  /* --- the Archives modal lists the same project headers --- */
  X.state.archives = [FULL];
  X.state.archiveExpanded.add('a1');
  X.renderArchiveList();
  const modal = getEl('archive-list-items');
  const titles = modal.querySelectorAll('.archive-project-title').map((n) => n.textContent);
  check(titles.length === 3,
    'the Archives modal lists one project header per project, got ' + JSON.stringify(titles));
  check(titles[0] && titles[0].indexOf('Alpha') === 0,
    'the modal\'s first project header names Alpha, got ' + JSON.stringify(titles[0]));
  check(titles[2] && titles[2].indexOf('No project') === 0,
    'the modal\'s last project header is "No project", got ' + JSON.stringify(titles[2]));
  const modalCards = modal.querySelectorAll('.archive-card');
  check(modalCards.length === 3,
    'all three archived cards still render in the modal, got ' + modalCards.length);

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
