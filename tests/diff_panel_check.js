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
const docListeners = {};
/* What GET /api/cards answers (the board's own poll). Mutated in place: a poll
 * already in flight holds a reference to the array. */
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

const src = fs.readFileSync(APP_JS, 'utf8');
vm.runInContext(src + '\n;globalThis.__x = { state, classifyDiffLine, renderDiff, '
  + 'renderDiffVerdict, renderKeysPanel, KEYBINDINGS, DIFF_RANGES, '
  + 'openDiff, closeDiff, openActivity, closeActivity, buildCard, closeEditor, '
  + 'DIFF_WIDTH, DIFF_HEIGHT };', sandbox,
  { filename: 'app.js' });
const X = sandbox.__x;
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

return { ok: failures.length === 0, failures };
}

main().then((result) => {
  console.log(JSON.stringify(result, null, 2));
  process.exit(result.ok ? 0 : 1);
}).catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(2);
});
