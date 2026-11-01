/* Diff-panel + Keys-panel reproducer for card b4e872.
 *
 * Usage: node tests/diff_panel_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   - the diff is rendered with a small line classifier (hunk header /
 *     addition / deletion / context), and every line is inserted as a TEXT
 *     NODE — the content is model-written and must never go through innerHTML;
 *   - the verdict badge, the reviewer's reason, the round, the model and the
 *     token count are shown next to the diff;
 *   - the file list shows +/- counts and clicking a file scrolls to it;
 *   - the Keys panel is generated from KEYBINDINGS and lists N, Esc and D.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: diff_panel_check.js <app.js>'); process.exit(2); }

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
  if (sel.startsWith('[')) {
    const m = /^\[([\w-]+)="(.*)"\]$/.exec(sel);
    if (!m) return false;
    // data-path -> dataset.path (the DOM's camelCase mapping)
    const key = m[1].replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return n.dataset && n.dataset[key] === m[2];
  }
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
    scrollTop: 0, scrollWidth: 0, scrolledIntoView: false,
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
  node.scrollIntoView = () => { node.scrolledIntoView = true; };
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
    querySelector: () => null,
    querySelectorAll: () => [],
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
vm.runInContext(src + '\n;globalThis.__x = { state, classifyDiffLine, renderDiff, '
  + 'renderDiffVerdict, renderKeysPanel, KEYBINDINGS, DIFF_RANGES };', sandbox,
  { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.renderDiff !== 'function' || typeof X.classifyDiffLine !== 'function') {
  console.error('could not load renderDiff/classifyDiffLine from app.js');
  process.exit(2);
}

/* ---------- checks ---------- */
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
/* All text under a node (the stub's textContent does not aggregate children). */
function textOf(node) {
  if (!node) return '';
  if (node.tagName === '#text') return node.textContent;
  return (node.textContent || '') + node.children.map(textOf).join(' ');
}

/* 1. The line classifier. */
const cases = [
  ['@@ -1,3 +1,4 @@', 'hunk'],
  ['+++ b/hello.py', 'meta'],
  ['--- a/hello.py', 'meta'],
  ['diff --git a/hello.py b/hello.py', 'meta'],
  ['index 1234567..89abcde 100644', 'meta'],
  ['+print("hi")', 'add'],
  ['-print("bye")', 'del'],
  [' print("same")', 'ctx'],
  ['', 'ctx'],
];
for (const [line, kind] of cases) {
  const got = X.classifyDiffLine(line);
  check(got === kind, 'classifyDiffLine(' + JSON.stringify(line) + ') should be ' + kind + ', got ' + got);
}

/* 2. Rendering: one row per line, classified, every line a TEXT NODE. */
const diff = [
  'diff --git a/hello.py b/hello.py',
  'index 1111111..2222222 100644',
  '--- a/hello.py',
  '+++ b/hello.py',
  '@@ -1 +1,2 @@',
  ' print("hello")',
  '+print("world")',
  '-print("old")',
].join('\n');
X.renderDiff({
  range: 'card', base: 'aaa', head: 'bbb', diff, truncated: false,
  files: [{ path: 'hello.py', additions: 1, deletions: 1 }],
  review: { approved: true, reason: 'looks good', round: 2, model: 'fake-model',
            tokens: 42, via: 'submit_verdict' },
});
const body = getEl('diff-body');
const rows = body.children.filter((c) => c.classList.contains('diff-line'));
check(rows.length === 8, 'one row per diff line, got ' + rows.length);
const kinds = rows.map((r) => (r.className.match(/diff-line--(\w+)/) || [])[1]);
check(JSON.stringify(kinds) === JSON.stringify(['meta', 'meta', 'meta', 'meta', 'hunk', 'ctx', 'add', 'del']),
  'rows are classified in order, got ' + JSON.stringify(kinds));
// Every row's content is a text node, never markup.
for (const row of rows) {
  check(row.children.length === 1 && row.children[0].tagName === '#TEXT',
    'each diff line is a single TEXT NODE, got ' + JSON.stringify(row.children.map((c) => c.tagName)));
}
check(rows[6].children[0].textContent === '+print("world")',
  'the addition row carries the raw line, got ' + JSON.stringify(rows[6].children[0].textContent));
// The file header row is anchored, so the file list can scroll to it.
check(rows[0].dataset.path === 'hello.py',
  'the diff --git row carries the file path, got ' + JSON.stringify(rows[0].dataset.path));

/* 3. The verdict is pinned next to the diff. */
const verdict = getEl('diff-verdict');
const verdictText = textOf(verdict);
check(/approved/.test(verdictText), 'the verdict badge says approved, got ' + JSON.stringify(verdictText));
check(verdictText.indexOf('looks good') !== -1, 'the reviewer reason is shown, got ' + JSON.stringify(verdictText));
check(verdictText.indexOf('round 2') !== -1, 'the round is shown, got ' + JSON.stringify(verdictText));
check(verdictText.indexOf('fake-model') !== -1, 'the model is shown, got ' + JSON.stringify(verdictText));
check(verdictText.indexOf('42') !== -1, 'the token count is shown, got ' + JSON.stringify(verdictText));

/* 4. A rejected verdict reads as rejected. */
X.renderDiffVerdict({ approved: false, reason: 'nope', round: 1, tokens: 3 });
const rejected = textOf(getEl('diff-verdict'));
check(/rejected/.test(rejected), 'a rejected verdict says rejected, got ' + JSON.stringify(rejected));

/* 5. The file list shows +/- counts and clicking a file scrolls to it. */
const files = getEl('diff-files');
const fileRows = files.children.filter((c) => c.classList.contains('diff-file'));
check(fileRows.length === 1, 'one file row, got ' + fileRows.length);
const fileText = textOf(fileRows[0]);
check(fileText.indexOf('hello.py') !== -1 && fileText.indexOf('+1') !== -1 && fileText.indexOf('1') !== -1,
  'the file row shows the path and the +/- counts, got ' + JSON.stringify(fileText));
fileRows[0].listeners.click[0]();
check(rows[0].scrolledIntoView === true, 'clicking a file scrolls to its first row');

/* 6. A sandbox card (no diff) explains itself instead of rendering nothing. */
X.renderDiff({ range: 'card', diff: null, truncated: false, files: [], review: null,
               reason: 'not git-backed' });
const empty = textOf(getEl('diff-body'));
check(empty.indexOf('not git-backed') !== -1, 'a non-git card explains itself, got ' + JSON.stringify(empty));

/* 7. The Keys panel lists every keybinding, from the one list. */
const keys = X.KEYBINDINGS.map((b) => b.key);
check(keys.indexOf('N') !== -1 && keys.indexOf('Esc') !== -1 && keys.indexOf('D') !== -1,
  'KEYBINDINGS lists N, Esc and D, got ' + JSON.stringify(keys));
X.renderKeysPanel();
const keysBody = getEl('sidebar-body');
const keyRows = keysBody.querySelectorAll('.keys-row');
check(keyRows.length === X.KEYBINDINGS.length,
  'the Keys panel renders one row per binding, got ' + keyRows.length);
const keyText = keyRows.map(textOf).join(' | ');
for (const k of ['N', 'Esc', 'D']) {
  check(keyText.indexOf(k) !== -1, 'the Keys panel lists ' + k + ', got ' + JSON.stringify(keyText));
}

/* 8. The range selector offers card / round / worktree. */
const ranges = X.DIFF_RANGES.map((r) => r.key);
check(JSON.stringify(ranges) === JSON.stringify(['card', 'round', 'worktree']),
  'the range selector offers card/round/worktree, got ' + JSON.stringify(ranges));

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);
