/* Diff-popup + Keys-panel reproducer for cards b4e872 and 6b29b4.
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
 *   - the diff opens as a FLOATING panel (hold D + click a card), not a
 *     full-screen overlay: ~720x640, clamped to the viewport, near the card,
 *     draggable by its header, closed with Esc;
 *   - a plain click still opens the editor, C + click still opens the live
 *     activity popup, and only one popup is open at a time;
 *   - the old hover-then-press-D gesture is gone;
 *   - the Keys panel is generated from KEYBINDINGS and lists N, Esc, D + click
 *     and C + click.
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
  Object.defineProperty(node, 'nextSibling', {
    get() {
      if (!node.parentNode) return null;
      const i = node.parentNode.children.indexOf(node);
      return i >= 0 ? node.parentNode.children[i + 1] || null : null;
    },
  });
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

const src = fs.readFileSync(APP_JS, 'utf8');

/* Boot the served app.js in a fresh sandbox. `store` is the localStorage
 * backing, so a second boot with the same store is a reload. */
function bootApp(store) {
  const byId = {};
  const getEl = (id) => { if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; } return byId[id]; };
  const docListeners = {};
  /* What GET /api/cards answers (the board's own poll). Mutated in place: a
   * poll already in flight holds a reference to the array. */
  const boardCards = [];
  const fetched = [];
  const sandbox = {
    console,
    document: {
      getElementById: getEl,
      createElement: (t) => makeEl(t),
      createElementNS: (ns, t) => makeEl(t),
      createTextNode: (t) => { const n = makeEl('#text'); n.textContent = t; return n; },
      addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      querySelector: () => null,
      querySelectorAll: () => [],
      body: makeEl('body'),
    },
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    fetch: async (url) => {
      fetched.push(String(url));
      const path = String(url).split('?')[0];
      // The board's own poll must get a LIST back, or it would overwrite
      // state.cards with an object mid-test.
      const body = path === '/api/cards' ? boardCards
        : ['/api/stacks', '/api/projects', '/api/templates', '/api/archives'].includes(path) ? []
        : {};
      return { ok: true, status: 200, statusText: 'OK', json: async () => body, text: async () => '' };
    },
    requestAnimationFrame: (fn) => fn(),
    getComputedStyle: () => ({ fontSize: '15px' }),
    CSS: { escape: (s) => String(s) },
    navigator: { platform: 'test', userAgentData: null },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    innerWidth: 1280, innerHeight: 800,
    Date, Math, JSON, Promise, Set, Map, Array, Object, String, Number, Boolean, parseFloat, parseInt, isNaN, RegExp, Error,
  };
  sandbox.addEventListener = () => {};
  sandbox.confirm = () => true;
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src + '\n;globalThis.__x = { state, classifyDiffLine, parseDiff, '
    + 'diffWords, alignHunkRows, collapseContext, collapsePairs, effectiveDiffMode, '
    + 'loadDiffPalette, saveDiffPalette, DIFF_MODES, DIFF_COLLAPSE_AFTER, '
    + 'DIFF_SPLIT_MIN_WIDTH, renderDiff, highlightLine, highlightLang, '
    + 'renderDiffVerdict, renderKeysPanel, KEYBINDINGS, DIFF_RANGES, '
    + 'openDiff, closeDiff, openActivity, closeActivity, buildCard, closeEditor, '
    + 'DIFF_WIDTH, DIFF_HEIGHT };', sandbox,
    { filename: 'app.js' });
  return { sandbox, X: sandbox.__x, getEl, store, docListeners, boardCards, fetched };
}

const app = bootApp({});
const X = app.X;
const getEl = app.getEl;
const store = app.store;
const docListeners = app.docListeners;
const boardCards = app.boardCards;
const fetched = app.fetched;
const sandbox = app.sandbox;
if (!X || typeof X.renderDiff !== 'function' || typeof X.classifyDiffLine !== 'function') {
  console.error('could not load renderDiff/classifyDiffLine from app.js');
  process.exit(2);
}

/* The real page ships every modal with the `hidden` attribute; the stub
 * creates elements visible, so hide them here (Esc must reach the popups). */
for (const id of ['editor-overlay', 'log-overlay', 'projects-overlay',
                  'diff-popup', 'activity-popup']) {
  getEl(id).hidden = true;
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
/* The text of one code cell, with no separator between the spans: a token
 * split must not change what the line says. */
function codeText(node) {
  if (!node) return '';
  if (node.tagName === '#text') return node.textContent;
  return (node.textContent || '') + node.children.map(codeText).join('');
}
/* Every leaf under a code cell must be a TEXT NODE: the diff is model-written,
 * so the renderer may wrap it in a word-diff span or a token span, never in
 * markup it parsed. */
function textOnlyLeaves(node, where) {
  for (const child of node.children) {
    if (child.tagName === '#TEXT') continue;
    const cls = child.className || '';
    const allowed = cls.split(/\s+/).some((c) => c === 'diff-word'
      || c.startsWith('diff-word--') || c.startsWith('tok-'));
    check(allowed, where + ': unexpected element ' + child.tagName
      + ' class=' + JSON.stringify(cls));
    textOnlyLeaves(child, where);
  }
}
function fireDoc(type, event) {
  for (const fn of (docListeners[type] || [])) fn(event);
}
function keyEvent(key) {
  return { key, target: { tagName: 'BODY' }, preventDefault: () => {} };
}
function setBoard(cards) {
  boardCards.length = 0;
  boardCards.push(...cards);
  X.state.cards = boardCards;
}

async function main() {
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

/* 1a. The parser: files -> hunks -> rows, each row carrying its old/new line
 *     number. The numbers must be right across hunks, not just in the first. */
const parsed = X.parseDiff([
  'diff --git a/hello.py b/hello.py',
  'index 1111111..2222222 100644',
  '--- a/hello.py',
  '+++ b/hello.py',
  '@@ -1,3 +1,4 @@',
  ' one',
  '-two',
  '+TWO',
  '+three',
  ' four',
  '@@ -10,2 +11,2 @@',
  ' ten',
  '-eleven',
  '+ELEVEN',
].join('\n'));
check(parsed.length === 1, 'one file parsed, got ' + parsed.length);
const parsedFile = parsed[0];
check(parsedFile.path === 'hello.py', 'the file path is parsed, got ' + JSON.stringify(parsedFile.path));
const hunkBlocks = parsedFile.blocks.filter((b) => b.type === 'hunk');
check(hunkBlocks.length === 2, 'two hunks parsed, got ' + hunkBlocks.length);
const firstRows = hunkBlocks[0].rows;
const secondRows = hunkBlocks[1].rows;
const lineOf = (rows, kind, text) => rows.find((r) => r.kind === kind && r.text === text);
check(lineOf(firstRows, 'ctx', 'one').oldLine === 1 && lineOf(firstRows, 'ctx', 'one').newLine === 1,
  'the first context line is 1/1');
check(lineOf(firstRows, 'del', 'two').oldLine === 2 && lineOf(firstRows, 'del', 'two').newLine === null,
  'the deletion is old line 2');
check(lineOf(firstRows, 'add', 'TWO').newLine === 2 && lineOf(firstRows, 'add', 'TWO').oldLine === null,
  'the addition is new line 2');
check(lineOf(firstRows, 'add', 'three').newLine === 3, 'the second addition is new line 3');
check(lineOf(firstRows, 'ctx', 'four').oldLine === 3 && lineOf(firstRows, 'ctx', 'four').newLine === 4,
  'the trailing context is 3/4');
check(lineOf(secondRows, 'ctx', 'ten').oldLine === 10 && lineOf(secondRows, 'ctx', 'ten').newLine === 11,
  'the second hunk starts at 10/11');
check(lineOf(secondRows, 'del', 'eleven').oldLine === 11, 'the second deletion is old line 11');
check(lineOf(secondRows, 'add', 'ELEVEN').newLine === 12, 'the second addition is new line 12');

/* 1b. Word-level diff: a one-character change marks the character, not the
 *     whole line. */
const wordOps = X.diffWords('cat', 'cut');
const opText = (op) => wordOps.filter((o) => o.op === op).map((o) => o.text).join('');
check(opText('del') === 'a', 'the deleted character is a, got ' + JSON.stringify(opText('del')));
check(opText('add') === 'u', 'the added character is u, got ' + JSON.stringify(opText('add')));
check(opText('eq') === 'ct', 'the unchanged characters are ct, got ' + JSON.stringify(opText('eq')));

/* 1c. A trailing newline is not a phantom context line. */
const trailing = X.parseDiff([
  'diff --git a/t.txt b/t.txt',
  '--- a/t.txt',
  '+++ b/t.txt',
  '@@ -1 +1 @@',
  '-a',
  '+b',
  '',
].join('\n'));
const trailingRows = trailing[0].blocks.find((b) => b.type === 'hunk').rows;
check(trailingRows.length === 2, 'the trailing newline adds no row, got ' + trailingRows.length);
check(trailingRows[1].newLine === 1, 'the last line number is not bumped, got ' + trailingRows[1].newLine);

/* 1d. `\ No newline at end of file` is meta, not a context line. */
const noNewlineText = [
  'diff --git a/n.txt b/n.txt',
  '--- a/n.txt',
  '+++ b/n.txt',
  '@@ -1 +1 @@',
  '-a',
  '\\ No newline at end of file',
  '+b',
  '\\ No newline at end of file',
].join('\n');
const noNewline = X.parseDiff(noNewlineText);
const noNewlineRows = noNewline[0].blocks.find((b) => b.type === 'hunk').rows;
check(noNewlineRows.length === 4, 'the no-newline markers are rows, got ' + noNewlineRows.length);
check(noNewlineRows[1].kind === 'meta' && noNewlineRows[3].kind === 'meta',
  'the no-newline markers are meta');
check(noNewlineRows[2].newLine === 1,
  'the marker does not bump the line number, got ' + noNewlineRows[2].newLine);

/* 1e. Inside a hunk, `---` is a deleted line, not a file header. */
const dashes = X.parseDiff([
  'diff --git a/doc.md b/doc.md',
  '--- a/doc.md',
  '+++ b/doc.md',
  '@@ -1,2 +1,2 @@',
  '----',
  '++++',
  ' text',
].join('\n'));
const dashRows = dashes[0].blocks.find((b) => b.type === 'hunk').rows;
check(dashRows[0].kind === 'del' && dashRows[0].text === '---',
  'a deleted --- line is a deletion, got ' + JSON.stringify(dashRows[0]));
check(dashRows[1].kind === 'add' && dashRows[1].text === '+++',
  'an added +++ line is an addition, got ' + JSON.stringify(dashRows[1]));

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
X.state.diffMode = 'unified';
X.renderDiff({
  range: 'card', base: 'aaa', head: 'bbb', diff, truncated: false,
  files: [{ path: 'hello.py', additions: 1, deletions: 1 }],
  review: { approved: true, reason: 'looks good', round: 2, model: 'fake-model',
            tokens: 42, via: 'submit_verdict' },
});
const body = getEl('diff-body');
const rows = body.querySelectorAll('.diff-row');
check(rows.length === 8, 'one row per diff line, got ' + rows.length);
const kinds = rows.map((r) => (r.className.match(/diff-row--(\w+)/) || [])[1]);
check(JSON.stringify(kinds) === JSON.stringify(['meta', 'meta', 'meta', 'meta', 'hunk', 'ctx', 'add', 'del']),
  'rows are classified in order, got ' + JSON.stringify(kinds));
// Every row's content is a text node (or a word/token span around one), never
// markup.
for (const row of rows) {
  const code = row.querySelector('.diff-code');
  textOnlyLeaves(code || row, 'row ' + row.className);
}
check(codeText(rows[6].querySelector('.diff-code')) === 'print("world")',
  'the addition row carries the line without its marker, got '
  + JSON.stringify(codeText(rows[6].querySelector('.diff-code'))));
// The file block is anchored, so the file list can scroll to it.
const fileBlock = body.querySelector('.diff-file-block');
check(fileBlock.dataset.path === 'hello.py',
  'the file block carries the file path, got ' + JSON.stringify(fileBlock.dataset.path));

/* 2a0. The file header shows the path and +N -M, and collapses its rows. */
const fileHead = fileBlock.querySelector('.diff-file-head');
check(textOf(fileHead).indexOf('hello.py') !== -1, 'the file header shows the path');
check(textOf(fileHead).indexOf('+1') !== -1 && textOf(fileHead).indexOf('−1') !== -1,
  'the file header shows +N -M, got ' + JSON.stringify(textOf(fileHead)));
const fileRowsEl = fileBlock.querySelector('.diff-file-rows');
check(fileRowsEl.hidden === false, 'the file rows start visible');
fileHead.listeners.click[0]();
check(fileRowsEl.hidden === true, 'clicking the file header collapses it');
check(fileHead.getAttribute('aria-expanded') === 'false', 'the collapsed header says so');
fileHead.listeners.click[0]();
check(fileRowsEl.hidden === false, 'clicking again expands it');

/* 2a. Line numbers: both sides, in a fixed-width gutter, correct across hunks. */
const ctxRow = rows[5];
const ctxLns = ctxRow.querySelectorAll('.diff-ln');
check(ctxLns.length === 2, 'a unified row has two line numbers, got ' + ctxLns.length);
check(ctxLns[0].textContent === '1' && ctxLns[1].textContent === '1',
  'the context row shows old 1 / new 1, got ' + JSON.stringify(ctxLns.map((n) => n.textContent)));
const addRow = rows[6];
const addLns = addRow.querySelectorAll('.diff-ln');
check(addLns[0].textContent === '' && addLns[1].textContent === '2',
  'the addition row shows only the new line number, got ' + JSON.stringify(addLns.map((n) => n.textContent)));
const delRow = rows[7];
const delLns = delRow.querySelectorAll('.diff-ln');
check(delLns[0].textContent === '2' && delLns[1].textContent === '',
  'the deletion row shows only the old line number, got ' + JSON.stringify(delLns.map((n) => n.textContent)));

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
check(fileBlock.scrolledIntoView === true, 'clicking a file scrolls to its block');

/* 2b. A one-character change marks the character inside the line. */
X.renderDiff({
  range: 'card', diff: [
    'diff --git a/word.txt b/word.txt',
    '--- a/word.txt',
    '+++ b/word.txt',
    '@@ -1 +1 @@',
    '-cat',
    '+cut',
  ].join('\n'),
  truncated: false, files: [{ path: 'word.txt', additions: 1, deletions: 1 }], review: null,
});
const wordRows = getEl('diff-body').querySelectorAll('.diff-row');
const wordDel = wordRows.find((r) => r.classList.contains('diff-row--del'));
const wordAdd = wordRows.find((r) => r.classList.contains('diff-row--add'));
const delSpan = wordDel.querySelector('.diff-word--del');
const addSpan = wordAdd.querySelector('.diff-word--add');
check(delSpan && textOf(delSpan) === 'a',
  'the deleted character is highlighted, got ' + JSON.stringify(delSpan && textOf(delSpan)));
check(addSpan && textOf(addSpan) === 'u',
  'the added character is highlighted, got ' + JSON.stringify(addSpan && textOf(addSpan)));
check(delSpan.children.length === 1 && delSpan.children[0].tagName === '#TEXT',
  'the highlighted character is a TEXT NODE');

/* 2c. A 40-line unchanged run collapses and expands. */
const longCtx = [];
for (let i = 0; i < 40; i++) longCtx.push(' line ' + i);
X.renderDiff({
  range: 'card', diff: [
    'diff --git a/long.txt b/long.txt',
    '--- a/long.txt',
    '+++ b/long.txt',
    '@@ -1,41 +1,41 @@',
    ...longCtx,
    '-old',
    '+new',
  ].join('\n'),
  truncated: false, files: [{ path: 'long.txt', additions: 1, deletions: 1 }], review: null,
});
const longBody = getEl('diff-body');
const collapsed = longBody.querySelector('.diff-collapsed');
check(!!collapsed, 'a 40-line unchanged run collapses');
check(textOf(collapsed).indexOf('40 unchanged lines') !== -1,
  'the collapsed row says how many lines, got ' + JSON.stringify(textOf(collapsed)));
const collapsedGroup = collapsed.nextSibling;
check(collapsedGroup && collapsedGroup.hidden === true, 'the unchanged rows start hidden');
collapsed.listeners.click[0]();
check(collapsedGroup.hidden === false, 'clicking the collapsed row expands it');
check(collapsed.getAttribute('aria-expanded') === 'true', 'the expanded row says so');
collapsed.listeners.click[0]();
check(collapsedGroup.hidden === true, 'clicking again collapses it');

/* 2c0. The threshold is "more than 6": six stays visible, seven collapses. */
const sixCtx = [];
for (let i = 0; i < 6; i++) sixCtx.push(' line ' + i);
X.renderDiff({
  range: 'card', diff: [
    'diff --git a/six.txt b/six.txt',
    '--- a/six.txt',
    '+++ b/six.txt',
    '@@ -1,7 +1,7 @@',
    ...sixCtx,
    '-old',
    '+new',
  ].join('\n'),
  truncated: false, files: [], review: null,
});
check(!getEl('diff-body').querySelector('.diff-collapsed'),
  'six unchanged lines stay visible');
X.renderDiff({
  range: 'card', diff: [
    'diff --git a/seven.txt b/seven.txt',
    '--- a/seven.txt',
    '+++ b/seven.txt',
    '@@ -1,8 +1,8 @@',
    ...sixCtx,
    ' line 6',
    '-old',
    '+new',
  ].join('\n'),
  truncated: false, files: [], review: null,
});
check(!!getEl('diff-body').querySelector('.diff-collapsed'),
  'seven unchanged lines collapse');

/* 2d. Side-by-side: old left, new right, aligned by hunk. */
X.state.diffMode = 'split';
X.renderDiff({
  range: 'card', diff: [
    'diff --git a/split.txt b/split.txt',
    '--- a/split.txt',
    '+++ b/split.txt',
    '@@ -1,2 +1,2 @@',
    ' same',
    '-old',
    '+new',
  ].join('\n'),
  truncated: false, files: [{ path: 'split.txt', additions: 1, deletions: 1 }], review: null,
});
const splitRows = getEl('diff-body').querySelectorAll('.diff-row--split');
check(splitRows.length === 2, 'two aligned rows (one context, one change), got ' + splitRows.length);
const ctxCells = splitRows[0].querySelectorAll('.diff-cell');
check(ctxCells.length === 2, 'a split row has two cells, got ' + ctxCells.length);
check(textOf(ctxCells[0].querySelector('.diff-code')) === 'same'
      && textOf(ctxCells[1].querySelector('.diff-code')) === 'same',
  'the context row is on both sides');
const changeCells = splitRows[1].querySelectorAll('.diff-cell');
check(changeCells[0].classList.contains('diff-cell--del'), 'the left cell is the deletion');
check(changeCells[1].classList.contains('diff-cell--add'), 'the right cell is the addition');
check(textOf(changeCells[0].querySelector('.diff-code')) === 'old'
      && textOf(changeCells[1].querySelector('.diff-code')) === 'new',
  'the change is old left / new right');
const leftLn = changeCells[0].querySelector('.diff-ln');
const rightLn = changeCells[1].querySelector('.diff-ln');
check(leftLn.textContent === '2' && rightLn.textContent === '2',
  'the split row shows old 2 / new 2, got ' + JSON.stringify([leftLn.textContent, rightLn.textContent]));

/* 2e. The default layout follows the panel width: split when wide enough,
 *     unified on a narrow panel. */
X.state.diffMode = null;
getEl('diff-popup').style.width = '720px';
check(X.effectiveDiffMode() === 'split', 'a wide panel defaults to side-by-side');
getEl('diff-popup').style.width = '500px';
check(X.effectiveDiffMode() === 'unified', 'a narrow panel defaults to unified');
getEl('diff-popup').style.width = '720px';
X.state.diffMode = 'split';

/* 2f. The layout toggle switches unified <-> side-by-side. */
const viewBtn = (mode) => getEl('diff-views').children.find((c) => c.dataset.mode === mode);
check(!!viewBtn('unified') && !!viewBtn('split'), 'the layout buttons are unified and split');
viewBtn('unified').listeners.click[0]();
check(X.state.diffMode === 'unified', 'clicking Unified selects unified');
check(getEl('diff-body').querySelectorAll('.diff-row--split').length === 0,
  'unified mode has no split rows');
viewBtn('split').listeners.click[0]();
check(X.state.diffMode === 'split', 'clicking Side by side selects split');
check(getEl('diff-body').querySelectorAll('.diff-row--split').length > 0,
  'split mode renders split rows');

/* 2g. The colourblind palette toggles and persists across a reload. */
const paletteBtn = getEl('diff-palette');
check(X.state.diffPalette === 'default', 'the palette starts default, got ' + X.state.diffPalette);
paletteBtn.listeners.click[0]();
check(X.state.diffPalette === 'colorblind', 'clicking the palette selects colourblind');
check(getEl('diff-body').dataset.palette === 'colorblind', 'the body carries the palette');
check(store['vibe.diff.palette'] === 'colorblind',
  'the palette is persisted, got ' + JSON.stringify(store['vibe.diff.palette']));
check(paletteBtn.getAttribute('aria-pressed') === 'true', 'the palette button says it is on');
const reloaded = bootApp(store);
check(reloaded.X.state.diffPalette === 'colorblind',
  'a reload reads the persisted palette, got ' + reloaded.X.state.diffPalette);
paletteBtn.listeners.click[0]();
check(X.state.diffPalette === 'default', 'clicking again restores the default palette');
check(store['vibe.diff.palette'] === 'default', 'the default palette is persisted too');

/* 2h. The truncated flag still shows its notice. */
X.renderDiff({ range: 'card', diff: 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b',
               truncated: true, files: [{ path: 'x', additions: 1, deletions: 1 }], review: null });
check(textOf(getEl('diff-body')).indexOf('truncated') !== -1,
  'the truncated notice is shown');

/* 2i. A meta row inside a hunk (the no-newline marker) spans the full width. */
X.state.diffMode = 'unified';
X.renderDiff({ range: 'card', diff: noNewlineText, truncated: false, files: [], review: null });
const markerRows = getEl('diff-body').querySelectorAll('.diff-row--meta');
check(markerRows.length >= 2, 'the no-newline markers render as meta rows, got ' + markerRows.length);
check(!markerRows[markerRows.length - 1].querySelector('.diff-code'),
  'a meta row has no code cell');

/* 6. A sandbox card (no diff) explains itself instead of rendering nothing. */
X.renderDiff({ range: 'card', diff: null, truncated: false, files: [], review: null,
               reason: 'not git-backed' });
const empty = textOf(getEl('diff-body'));
check(empty.indexOf('not git-backed') !== -1, 'a non-git card explains itself, got ' + JSON.stringify(empty));

/* 7. The Keys panel lists every keybinding, from the one list. */
const keys = X.KEYBINDINGS.map((b) => b.key);
check(keys.indexOf('N') !== -1 && keys.indexOf('Esc') !== -1,
  'KEYBINDINGS lists N and Esc, got ' + JSON.stringify(keys));
check(keys.indexOf('D + click') !== -1,
  'KEYBINDINGS lists the D + click gesture, got ' + JSON.stringify(keys));
check(keys.indexOf('D') === -1, 'the bare D binding is gone, got ' + JSON.stringify(keys));
X.renderKeysPanel();
const keysBody = getEl('sidebar-body');
const keyRows = keysBody.querySelectorAll('.keys-row');
check(keyRows.length === X.KEYBINDINGS.length,
  'the Keys panel renders one row per binding, got ' + keyRows.length);
const keyText = keyRows.map(textOf).join(' | ');
for (const k of ['N', 'Esc', 'D + click', 'C + click']) {
  check(keyText.indexOf(k) !== -1, 'the Keys panel lists ' + k + ', got ' + JSON.stringify(keyText));
}

/* 8. The range selector offers card / round / worktree. */
const ranges = X.DIFF_RANGES.map((r) => r.key);
check(JSON.stringify(ranges) === JSON.stringify(['card', 'round', 'worktree']),
  'the range selector offers card/round/worktree, got ' + JSON.stringify(ranges));

/* 9. The diff opens as a floating panel, not a full-screen overlay: ~720x640,
 *    near the clicked card and clamped to the viewport. */
const popup = getEl('diff-popup');
const anchor = makeEl('article');
anchor.getBoundingClientRect = () => ({ left: 900, top: 300, width: 200, height: 140, right: 1100, bottom: 440 });
setBoard([{ id: 'card-1', title: 'Diff card', status: 'done', session: {}, questions: [] }]);
await X.openDiff('card-1', null, anchor);
check(popup.hidden === false, 'the diff popup is shown');
check(!popup.classList.contains('overlay'), 'the diff popup is not a full-screen overlay');
check(X.DIFF_WIDTH === 720 && X.DIFF_HEIGHT === 640,
  'the popup is ~720x640, got ' + X.DIFF_WIDTH + 'x' + X.DIFF_HEIGHT);
const left = parseFloat(popup.style.left);
const top = parseFloat(popup.style.top);
check(left >= 0 && left + X.DIFF_WIDTH <= 1280,
  'the popup is clamped horizontally, got left=' + left);
check(top >= 0 && top + X.DIFF_HEIGHT <= 800,
  'the popup is clamped vertically, got top=' + top);
check(left < 900, 'a card near the right edge opens the popup to its left, got left=' + left);
check(textOf(getEl('diff-heading')).indexOf('Diff card') !== -1,
  'the header shows the card title, got ' + JSON.stringify(textOf(getEl('diff-heading'))));

/* 9a. The range selector still works: clicking "round" refetches that range. */
const rangeButtons = getEl('diff-ranges').children.filter((c) => c.classList.contains('diff-range'));
check(rangeButtons.length === 3, 'three range buttons, got ' + rangeButtons.length);
fetched.length = 0;
rangeButtons[1].listeners.click[0]();
check(X.state.diffRange === 'round', 'clicking Round selects the round range, got ' + X.state.diffRange);
check(fetched.some((u) => u.indexOf('/diff?range=round') !== -1),
  'the round range is fetched, got ' + JSON.stringify(fetched));
X.state.diffRange = 'card';

/* 10. Draggable by its header — and a click on a header button is not a drag. */
const head = getEl('diff-popup-head');
check(!!(head.listeners.pointerdown && head.listeners.pointermove),
  'the header is the drag handle');
const startLeft = parseFloat(popup.style.left);
const startTop = parseFloat(popup.style.top);
head.listeners.pointerdown[0]({ button: 0, clientX: 100, clientY: 100, target: head, preventDefault: () => {} });
head.listeners.pointermove[0]({ clientX: 140, clientY: 130 });
check(parseFloat(popup.style.left) === startLeft + 40,
  'dragging moves the panel horizontally, got ' + popup.style.left);
check(parseFloat(popup.style.top) === startTop + 30,
  'dragging moves the panel vertically, got ' + popup.style.top);
head.listeners.pointerup[0]({});
const draggedLeft = parseFloat(popup.style.left);
head.listeners.pointermove[0]({ clientX: 900, clientY: 900 });
check(parseFloat(popup.style.left) === draggedLeft, 'releasing the header stops the drag');
const closeBtn = makeEl('button');
head.appendChild(closeBtn);
head.listeners.pointerdown[0]({ button: 0, clientX: 0, clientY: 0, target: closeBtn, preventDefault: () => {} });
head.listeners.pointermove[0]({ clientX: 500, clientY: 500 });
check(parseFloat(popup.style.left) === draggedLeft, 'a click on a header button does not drag the panel');

/* 11. The gesture: D + click opens the diff, a plain click opens the editor,
 *     C + click opens the activity popup. */
const card = X.buildCard({ id: 'card-1', title: 'Diff card', body: 'spec', status: 'done',
                           session: {}, questions: [] });
X.closeDiff();
X.closeActivity();
X.closeEditor();
card.listeners.click[0]({});
check(X.state.editorCardId === 'card-1', 'a plain click opens the editor');
check(X.state.diffCardId === null, 'a plain click does not open the diff');
X.closeEditor();
fireDoc('keydown', keyEvent('d'));
card.listeners.click[0]({});
check(X.state.diffCardId === 'card-1', 'D + click opens the diff popup');
check(X.state.editorCardId === null, 'D + click does not open the editor');
fireDoc('keyup', keyEvent('d'));
X.closeDiff();
card.listeners.click[0]({});
check(X.state.editorCardId === 'card-1', 'releasing D restores the plain click');
X.closeEditor();
fireDoc('keydown', keyEvent('c'));
card.listeners.click[0]({});
check(X.state.activityCardId === 'card-1', 'C + click opens the activity popup');
check(X.state.diffCardId === null, 'C + click does not open the diff');
fireDoc('keyup', keyEvent('c'));
X.closeActivity();

/* 12. The hover-then-press-D gesture is gone. */
check(!('hoverCardId' in X.state), 'state.hoverCardId is gone');
check(typeof sandbox.hoveredCardId === 'undefined', 'hoveredCardId is gone');
check(!/hoverCardId|hoveredCardId/.test(src), 'no hover-card code remains in app.js');
X.closeDiff();
X.closeEditor();
fireDoc('keydown', keyEvent('d'));
check(X.state.diffCardId === null, 'pressing D alone opens nothing');
fireDoc('keyup', keyEvent('d'));

/* 13. Only one popup at a time: opening one closes the other. */
X.openActivity('card-1', anchor);
check(getEl('activity-popup').hidden === false, 'the activity popup is open');
await X.openDiff('card-1', null, anchor);
check(getEl('activity-popup').hidden === true, 'opening the diff closes the activity popup');
check(X.state.activityCardId === null, 'opening the diff clears the activity card');
check(popup.hidden === false, 'the diff popup is open');
X.openActivity('card-1', anchor);
check(popup.hidden === true, 'opening the activity closes the diff popup');
check(X.state.diffCardId === null, 'opening the activity clears the diff card');
X.closeActivity();

/* 14. Esc and the × close the popup. */
// A click's openEditor() is async (it loads the templates first), so let any
// pending one settle before testing Esc — otherwise it reopens the editor
// mid-check and Esc closes that instead.
await new Promise((resolve) => setTimeout(resolve, 0));
X.closeEditor();
await X.openDiff('card-1', null, anchor);
check(popup.hidden === false, 'the popup is open before Esc');
fireDoc('keydown', keyEvent('Escape'));
check(popup.hidden === true, 'Esc closes the diff popup');
check(X.state.diffCardId === null, 'Esc clears the diff card');
await X.openDiff('card-1', null, anchor);
getEl('diff-close').listeners.click[0]();
check(popup.hidden === true, 'the × closes the diff popup');
check(X.state.diffCardId === null, 'the × clears the diff card');

/* 14a. A modal on top takes Esc first; the popup closes after it. */
await X.openDiff('card-1', null, anchor);
getEl('editor-overlay').hidden = false;
fireDoc('keydown', keyEvent('Escape'));
check(getEl('editor-overlay').hidden === true, 'Esc closes the modal on top first');
check(popup.hidden === false, 'the popup stays open while a modal is on top');
fireDoc('keydown', keyEvent('Escape'));
check(popup.hidden === true, 'the next Esc closes the popup');

/* 15. The editor's Diff button opens the same popup, and the modal gets out
 *     of its way (the popup sits below modals, so it would be hidden). */
X.state.editorCardId = 'card-1';
getEl('editor-overlay').hidden = false;
getEl('btn-diff').listeners.click[0]();
check(getEl('editor-overlay').hidden === true,
  'the editor closes when its Diff button opens the popup');
check(X.state.diffCardId === 'card-1', 'the Diff button opens the popup for the edited card');
X.closeDiff();

/* 16. Syntax highlighting: the code is coloured by language, not just by kind.
 *     The tokenizer is hand-written (no dependency) and carries multi-line
 *     state, so a Python docstring is a string on every line it spans. */
const HL = X.highlightLine;
check(typeof HL === 'function', 'highlightLine is exported');
const tokOf = (tokens, text) => tokens.find((t) => t.text === text);
const tokText = (tokens, cls) => tokens.filter((t) => t.cls === cls).map((t) => t.text).join('');
const allCls = (tokens, cls) => tokens.length > 0 && tokens.every((t) => t.cls === cls);

/* 16a. The language comes from the file path, defaulting to plain. */
check(X.highlightLang('app/diffs.py') === 'python', 'a .py path is python');
check(X.highlightLang('app/static/app.js') === 'javascript', 'a .js path is javascript');
check(X.highlightLang('app/static/style.css') === 'css', 'a .css path is css');
check(X.highlightLang('app/static/index.html') === 'html', 'a .html path is html');
check(X.highlightLang('docs/how-it-works.md') === 'markdown', 'a .md path is markdown');
check(X.highlightLang('package.json') === 'json', 'a .json path is json');
check(X.highlightLang('notes.unknown') === 'plain', 'an unknown extension is plain');
check(X.highlightLang(null) === 'plain', 'a missing path is plain');

/* 16b. Python: keywords, strings, comments, numbers, functions, operators and
 *      punctuation each get their own class. */
const pyTokens = HL('def greet(name):  # hi', 'python', {}).tokens;
check(tokOf(pyTokens, 'def') && tokOf(pyTokens, 'def').cls === 'tok-keyword',
  'def is a keyword, got ' + JSON.stringify(pyTokens));
check(tokOf(pyTokens, 'greet') && tokOf(pyTokens, 'greet').cls === 'tok-fn',
  'a called name is a function, got ' + JSON.stringify(pyTokens));
check(tokOf(pyTokens, '# hi') && tokOf(pyTokens, '# hi').cls === 'tok-comment',
  'a # comment is a comment, got ' + JSON.stringify(pyTokens));
check(tokOf(pyTokens, '(') && tokOf(pyTokens, '(').cls === 'tok-punct',
  'a paren is punctuation, got ' + JSON.stringify(pyTokens));
const pyString = HL('x = "hi"', 'python', {}).tokens;
check(tokOf(pyString, '"hi"') && tokOf(pyString, '"hi"').cls === 'tok-string',
  'a quoted string is a string, got ' + JSON.stringify(pyString));
const pyNumber = HL('x = 42', 'python', {}).tokens;
check(tokOf(pyNumber, '42') && tokOf(pyNumber, '42').cls === 'tok-number',
  'a number is a number, got ' + JSON.stringify(pyNumber));
const pyOp = HL('a + b', 'python', {}).tokens;
check(tokOf(pyOp, '+') && tokOf(pyOp, '+').cls === 'tok-op',
  'an operator is an operator, got ' + JSON.stringify(pyOp));

/* 16c. A multi-line Python docstring is a string on every line it spans. */
const doc1 = HL('"""Doc line one', 'python', {});
check(doc1.state.inString === '"""',
  'an open docstring is remembered, got ' + JSON.stringify(doc1.state));
check(allCls(doc1.tokens, 'tok-string'),
  'the opening docstring line is a string, got ' + JSON.stringify(doc1.tokens));
const doc2 = HL('still the docstring', 'python', doc1.state);
check(allCls(doc2.tokens, 'tok-string'),
  'the middle docstring line is a string, got ' + JSON.stringify(doc2.tokens));
const doc3 = HL('end of docstring"""', 'python', doc2.state);
check(allCls(doc3.tokens, 'tok-string'),
  'the closing docstring line is a string, got ' + JSON.stringify(doc3.tokens));
check(doc3.state.inString === null,
  'the closed docstring clears the state, got ' + JSON.stringify(doc3.state));

/* 16d. A JavaScript block comment spans lines too, and code after it is code. */
const js1 = HL('/* start', 'javascript', {});
check(js1.state.inBlockComment === true,
  'an open block comment is remembered, got ' + JSON.stringify(js1.state));
const js2 = HL('middle', 'javascript', js1.state);
check(allCls(js2.tokens, 'tok-comment'),
  'the middle block-comment line is a comment, got ' + JSON.stringify(js2.tokens));
const js3 = HL('end */ const x = 1;', 'javascript', js2.state);
check(js3.state.inBlockComment === false,
  'the closed block comment clears the state, got ' + JSON.stringify(js3.state));
check(tokOf(js3.tokens, 'const') && tokOf(js3.tokens, 'const').cls === 'tok-keyword',
  'code after the comment closes is code, got ' + JSON.stringify(js3.tokens));

/* 16e. The plain fallback is one token with no class. */
const plainTokens = HL('anything at all', 'plain', {}).tokens;
check(plainTokens.length === 1 && plainTokens[0].cls === ''
      && plainTokens[0].text === 'anything at all',
  'plain is one unclassed token, got ' + JSON.stringify(plainTokens));
const unknownTokens = HL('x = 1', 'cobol', {}).tokens;
check(unknownTokens.length === 1 && unknownTokens[0].cls === '',
  'an unknown language falls back to plain, got ' + JSON.stringify(unknownTokens));

/* 16f. Rendering: a Python diff shows keywords, strings and comments in
 *      distinct classes, and the docstring is a string on every line. */
X.state.diffMode = 'unified';
const pyDiff = [
  'diff --git a/hello.py b/hello.py',
  '--- a/hello.py',
  '+++ b/hello.py',
  '@@ -1,4 +1,4 @@',
  ' def greet(name):',
  '-    return "hi " + name  # note',
  '+    return f"hi {name}"  # note',
  ' """Doc line one',
  ' still the docstring',
  ' end of docstring"""',
].join('\n');
X.renderDiff({ range: 'card', diff: pyDiff, truncated: false,
               files: [{ path: 'hello.py', additions: 1, deletions: 1 }], review: null });
const pyRows = getEl('diff-body').querySelectorAll('.diff-row');
const rowWithCode = (text) => pyRows.find((r) => codeText(r.querySelector('.diff-code')) === text);
const defRow = rowWithCode('def greet(name):');
check(!!defRow, 'the def line renders');
const defCode = defRow.querySelector('.diff-code');
check(textOf(defCode.querySelector('.tok-keyword')) === 'def',
  'the def keyword is a token span, got ' + JSON.stringify(textOf(defCode)));
check(textOf(defCode.querySelector('.tok-fn')) === 'greet',
  'the function name is a token span, got ' + JSON.stringify(textOf(defCode)));
const addCode = rowWithCode('    return f"hi {name}"  # note').querySelector('.diff-code');
check(textOf(addCode.querySelector('.tok-keyword')) === 'return',
  'return is a keyword, got ' + JSON.stringify(textOf(addCode)));
check(!!addCode.querySelector('.tok-string'), 'the f-string is a string');
check(textOf(addCode.querySelector('.tok-comment')) === '# note',
  'the trailing comment is a comment, got ' + JSON.stringify(textOf(addCode)));
for (const line of ['"""Doc line one', 'still the docstring', 'end of docstring"""']) {
  const row = rowWithCode(line);
  check(!!row, 'the docstring line renders: ' + line);
  const spans = row.querySelector('.diff-code').children.filter((c) => c.tagName !== '#TEXT');
  check(spans.length > 0 && spans.every((s) => s.classList.contains('tok-string')),
    'every span on a docstring line is a string: ' + line);
}

/* 16g0. HTML, Markdown, JSON and CSS use their own rules too. */
const htmlTokens = HL('<div class="x">hi</div>', 'html', {}).tokens;
check(tokOf(htmlTokens, '<div') && tokOf(htmlTokens, '<div').cls === 'tok-tag',
  'an HTML tag is a tag, got ' + JSON.stringify(htmlTokens));
check(tokOf(htmlTokens, 'class') && tokOf(htmlTokens, 'class').cls === 'tok-attr',
  'an HTML attribute is an attribute, got ' + JSON.stringify(htmlTokens));
check(tokOf(htmlTokens, '"x"') && tokOf(htmlTokens, '"x"').cls === 'tok-string',
  'an HTML attribute value is a string, got ' + JSON.stringify(htmlTokens));
const mdTokens = HL('## Title', 'markdown', {}).tokens;
check(mdTokens.length === 1 && mdTokens[0].cls === 'tok-heading',
  'a Markdown heading is a heading, got ' + JSON.stringify(mdTokens));
const jsonTokens = HL('{"a": 1}', 'json', {}).tokens;
check(tokOf(jsonTokens, '"a"') && tokOf(jsonTokens, '"a"').cls === 'tok-attr',
  'a JSON key is an attribute, got ' + JSON.stringify(jsonTokens));
check(tokOf(jsonTokens, '1') && tokOf(jsonTokens, '1').cls === 'tok-number',
  'a JSON number is a number, got ' + JSON.stringify(jsonTokens));
const cssTokens = HL('@media (min-width: 600px) {', 'css', {}).tokens;
check(tokOf(cssTokens, '@media') && tokOf(cssTokens, '@media').cls === 'tok-keyword',
  'a CSS at-rule is a keyword, got ' + JSON.stringify(cssTokens));
check(tokOf(cssTokens, 'min-width') && tokOf(cssTokens, 'min-width').cls === 'tok-attr',
  'a hyphenated CSS property is an attribute, got ' + JSON.stringify(cssTokens));

/* 16g1. A `$` in a CSS/SCSS line is a variable, not a crash. The identifier
 *       guard and the identifier regex must accept the same first characters:
 *       when they disagreed, `$brand` threw and the whole diff failed to
 *       render. */
check(X.highlightLang('theme.scss') === 'css', 'a .scss path is css');
const scssDecl = HL('$brand: #4c8dff;', 'css', {}).tokens;
check(tokOf(scssDecl, '$brand') && tokOf(scssDecl, '$brand').cls === 'tok-attr',
  'an SCSS variable declaration is an attribute, got ' + JSON.stringify(scssDecl));
const scssUse = HL('.a { color: $brand; }', 'css', {}).tokens;
const scssValue = scssUse.find((t) => t.text.indexOf('$brand') !== -1);
check(scssValue && scssValue.cls === '',
  'an SCSS variable used as a value is plain, got ' + JSON.stringify(scssUse));

/* 16g2. An .scss file in a diff renders instead of taking the popup down. */
X.renderDiff({ range: 'card', diff: [
  'diff --git a/theme.scss b/theme.scss',
  '--- a/theme.scss',
  '+++ b/theme.scss',
  '@@ -1,2 +1,2 @@',
  '-$brand: #4c8dff;',
  '+$brand: #e08a2e;',
  ' .a { color: $brand; }',
].join('\n'), truncated: false, files: [], review: null });
const scssRows = getEl('diff-body').querySelectorAll('.diff-row');
const scssRow = scssRows.find((r) => codeText(r.querySelector('.diff-code')) === '$brand: #e08a2e;');
check(!!scssRow, 'an .scss diff renders');
check(textOf(scssRow.querySelector('.tok-attr')) === '$brand',
  'the SCSS variable is a token span, got ' + JSON.stringify(textOf(scssRow)));

/* 16g. A JavaScript and a CSS file in the same diff each use their own rules. */
const mixedDiff = [
  'diff --git a/app.js b/app.js',
  '--- a/app.js',
  '+++ b/app.js',
  '@@ -1 +1 @@',
  '-const x = 1; // note',
  '+const x = 2; // note',
  'diff --git a/site.css b/site.css',
  '--- a/site.css',
  '+++ b/site.css',
  '@@ -1 +1 @@',
  '-.a { color: red; }',
  '+.a { color: blue; }',
].join('\n');
X.renderDiff({ range: 'card', diff: mixedDiff, truncated: false,
               files: [{ path: 'app.js', additions: 1, deletions: 1 },
                       { path: 'site.css', additions: 1, deletions: 1 }], review: null });
const mixedRows = getEl('diff-body').querySelectorAll('.diff-row');
const mixedRow = (text) => mixedRows.find((r) => codeText(r.querySelector('.diff-code')) === text);
const jsRow = mixedRow('const x = 2; // note');
check(!!jsRow, 'the JS line renders');
check(textOf(jsRow.querySelector('.tok-keyword')) === 'const',
  'const is a JS keyword, got ' + JSON.stringify(textOf(jsRow)));
check(textOf(jsRow.querySelector('.tok-comment')) === '// note',
  'the JS comment is a comment, got ' + JSON.stringify(textOf(jsRow)));
const cssRow = mixedRow('.a { color: blue; }');
check(!!cssRow, 'the CSS line renders');
check(textOf(cssRow.querySelector('.tok-attr')) === 'color',
  'the CSS property is an attribute, got ' + JSON.stringify(textOf(cssRow)));
check(!cssRow.querySelector('.tok-keyword'),
  'the CSS line has no JS keyword, got ' + JSON.stringify(textOf(cssRow)));

/* 16h. An unknown extension renders plain, with no errors. */
X.renderDiff({ range: 'card', diff: [
  'diff --git a/notes.unknown b/notes.unknown',
  '--- a/notes.unknown',
  '+++ b/notes.unknown',
  '@@ -1 +1 @@',
  '-plain text here',
  '+plain text there',
].join('\n'), truncated: false, files: [], review: null });
const unknownRow = getEl('diff-body').querySelectorAll('.diff-row')
  .find((r) => codeText(r.querySelector('.diff-code')) === 'plain text there');
check(!!unknownRow, 'the unknown-extension line renders');
check(unknownRow.querySelector('.diff-code').children.every((c) => c.tagName === '#TEXT'
    || !(c.className || '').split(/\s+/).some((k) => k.startsWith('tok-'))),
  'an unknown extension renders no token spans, got '
  + JSON.stringify(unknownRow.querySelector('.diff-code').children.map((c) => c.className)));

/* 16i. Token colours and the word-diff tint coexist: the changed span keeps
 *      its token colour, the word tint stays on the span. */
X.renderDiff({ range: 'card', diff: [
  'diff --git a/w.py b/w.py',
  '--- a/w.py',
  '+++ b/w.py',
  '@@ -1 +1 @@',
  '-return "cat"',
  '+return "cut"',
].join('\n'), truncated: false, files: [], review: null });
const wRows = getEl('diff-body').querySelectorAll('.diff-row');
const wAdd = wRows.find((r) => r.classList.contains('diff-row--add'));
const wWord = wAdd.querySelector('.diff-word--add');
check(!!wWord, 'the changed span is still marked');
check(textOf(wWord) === 'u', 'the changed character is still the changed span, got '
  + JSON.stringify(textOf(wWord)));
check(!!wWord.querySelector('.tok-string'),
  'the changed span keeps its token colour, got '
  + JSON.stringify(wWord.children.map((c) => c.className)));

/* 16k. Side-by-side keeps a state per column: the old side's docstring and the
 *      new side's docstring are each coloured on every line. */
X.state.diffMode = 'split';
X.renderDiff({ range: 'card', diff: [
  'diff --git a/doc.py b/doc.py',
  '--- a/doc.py',
  '+++ b/doc.py',
  '@@ -1,3 +1,3 @@',
  ' """old doc',
  '-middle old',
  '+middle new',
  ' end"""',
].join('\n'), truncated: false, files: [], review: null });
const splitDocRows = getEl('diff-body').querySelectorAll('.diff-row--split');
const splitCells = splitDocRows.flatMap((r) => r.querySelectorAll('.diff-cell'));
const cellWithCode = (text) => splitCells.find((c) => codeText(c.querySelector('.diff-code')) === text);
/* Every token class used under a node (a word-diff span may wrap one). */
const tokClasses = (node) => {
  const out = [];
  const walk = (n) => {
    for (const c of n.children) {
      out.push(...(c.className || '').split(/\s+/).filter((k) => k.startsWith('tok-')));
      walk(c);
    }
  };
  walk(node);
  return out;
};
for (const line of ['"""old doc', 'middle old', 'middle new', 'end"""']) {
  const cell = cellWithCode(line);
  check(!!cell, 'the split cell renders: ' + line);
  const classes = tokClasses(cell);
  check(classes.length > 0 && classes.every((c) => c === 'tok-string'),
    'the split cell is a string on every line: ' + line + ', got ' + JSON.stringify(classes));
}
X.state.diffMode = 'unified';

/* 16j. A 4000-line diff renders without a visible stall. */
const bigDiff = ['diff --git a/big.py b/big.py', '--- a/big.py', '+++ b/big.py',
                 '@@ -1,4000 +1,4000 @@'];
for (let i = 0; i < 4000; i++) bigDiff.push(' def f' + i + '(x):  # comment ' + i);
const bigStart = Date.now();
X.renderDiff({ range: 'card', diff: bigDiff.join('\n'), truncated: false,
               files: [{ path: 'big.py', additions: 0, deletions: 0 }], review: null });
const bigMs = Date.now() - bigStart;
check(bigMs < 1000, 'a 4000-line diff renders in well under a second, took ' + bigMs + 'ms');
check(getEl('diff-body').querySelectorAll('.tok-keyword').length > 0,
  'the big diff is highlighted');

/* 16l. The scanner is total: no character it accepts may throw. A guard that
 *      accepts a character its regex cannot match (the `$` in CSS) used to
 *      take the whole popup down, so every printable character is fed to
 *      every language. */
const PRINTABLE = Array.from({ length: 95 }, (_, k) => String.fromCharCode(32 + k)).join('');
for (const lang of ['python', 'javascript', 'css', 'html', 'markdown', 'json', 'plain']) {
  let threw = null;
  try {
    for (const ch of PRINTABLE) HL(ch, lang, {});
    HL(PRINTABLE, lang, {});
  } catch (err) {
    threw = err;
  }
  check(!threw, 'highlightLine never throws on ' + lang + ': ' + (threw && threw.message));
}

return { ok: failures.length === 0, failures };
}

main().then((result) => {
  console.log(JSON.stringify(result, null, 2));
  process.exit(result.ok ? 0 : 1);
}).catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(2);
});
