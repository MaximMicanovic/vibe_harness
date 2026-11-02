/* Live activity popup reproducer for card e02cd4.
 *
 * Usage: node tests/activity_popup_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   - the activity feed renders one row per event (text / reasoning / tool /
 *     result / status), and every piece of journal text is a TEXT NODE — the
 *     journal is model-written and must never go through innerHTML;
 *   - reasoning is collapsed by default behind a "thinking…" toggle;
 *   - the popup is a small floating panel (not a full-screen overlay),
 *     positioned near the clicked card and clamped to the viewport;
 *   - holding C and clicking a card opens the popup, a plain click still
 *     opens the editor, and Esc closes the popup;
 *   - scrolling up stops the auto-scroll and offers "jump to latest";
 *   - polling stops when the card leaves running/reviewing;
 *   - the chat-mode toggle is gone (state, panel, rail button, CSS class) and
 *     the Keys panel lists the C + click gesture.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: activity_popup_check.js <app.js>'); process.exit(2); }

/* ---------- minimal DOM stub (enough for app.js to boot) ---------- */
function makeClassList() {
  const set = new Set();
  return {
    add: (...cs) => cs.forEach((c) => set.add(c)),
    remove: (...cs) => cs.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
    toggle: (c, force) => {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    },
    _set: set,
  };
}
function matchSimple(n, sel) {
  if (!n.classList) return false;
  if (sel.startsWith('.')) return sel.slice(1).split('.').every((c) => n.classList.contains(c));
  if (sel.startsWith('[')) {
    const m = /^\[([\w-]+)="(.*)"\]$/.exec(sel);
    if (!m) return false;
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
    scrollTop: 0, scrollWidth: 0, clientWidth: 200, scrolledIntoView: false,
    _html: null,
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
  // innerHTML is recorded, never parsed: the checks below assert nothing in
  // the activity feed ever goes through it.
  Object.defineProperty(node, 'innerHTML', {
    get() { return node._html || ''; },
    set(v) { node._html = String(v); },
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
const timers = { next: 1, cleared: [] };
let activityResponse = { cursor: 0, events: [] };
let activityFailure = null;   // null | 404 | 500 — make the next activity call fail
let boardCards = [];          // what GET /api/cards answers (the board's own poll)
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
    const isActivity = String(url).indexOf('/activity') !== -1;
    if (isActivity && activityFailure) {
      return { ok: false, status: activityFailure, statusText: 'error',
               json: async () => ({ detail: 'boom' }), text: async () => 'boom' };
    }
    // Capture the scripted body NOW: a test may swap activityResponse while
    // the request is in flight. The board's own poll must get a LIST back, or
    // it would overwrite state.cards with an object mid-test.
    const path = String(url).split('?')[0];
    const body = isActivity ? activityResponse
      : path === '/api/cards' ? boardCards
      : path === '/api/stacks' ? [] : {};
    return { ok: true, status: 200, statusText: 'OK', json: async () => body, text: async () => '' };
  },
  requestAnimationFrame: (fn) => fn(),
  getComputedStyle: () => ({ fontSize: '15px' }),
  CSS: { escape: (s) => String(s) },
  navigator: { platform: 'test', userAgentData: null },
  setInterval: () => timers.next++,
  clearInterval: (id) => { timers.cleared.push(id); },
  setTimeout: () => 0, clearTimeout: () => {},
  innerWidth: 1280, innerHeight: 800,
  Date, Math, JSON, Promise, Set, Map, Array, Object, String, Number, Boolean,
  parseFloat, parseInt, isNaN, RegExp, Error,
};
sandbox.addEventListener = () => {};
sandbox.confirm = () => true;
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(APP_JS, 'utf8');
vm.runInContext(src + '\n;globalThis.__x = { state, renderActivityEvents, activityEventRow, '
  + 'openActivity, closeActivity, pollActivity, positionFloatingPanel, '
  + 'scrollActivityToLatest, buildCard, closeEditor, KEYBINDINGS, SIDEBAR_PANELS, '
  + 'ACTIVITY_WIDTH, ACTIVITY_HEIGHT };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.renderActivityEvents !== 'function') {
  console.error('could not load renderActivityEvents from app.js');
  process.exit(2);
}

/* The board's own 2s poll keeps overwriting state.cards from GET /api/cards,
 * so the test card is set on BOTH sides — and the array is mutated in place,
 * because a poll already in flight holds a reference to it. */
function setBoard(cards) {
  boardCards.length = 0;
  boardCards.push(...cards);
  X.state.cards = boardCards;
}

/* The real page ships every modal with the `hidden` attribute; the stub
 * creates elements visible, so hide them here (Esc must reach the popup). */
for (const id of ['editor-overlay', 'log-overlay', 'diff-popup', 'projects-overlay']) {
  getEl(id).hidden = true;
}

/* ---------- checks ---------- */
async function main() {
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
function textOf(node) {
  if (!node) return '';
  if (node.tagName === '#text') return node.textContent;
  return (node.textContent || '') + node.children.map(textOf).join(' ');
}
function walk(node, fn) { fn(node); for (const c of node.children) walk(c, fn); }
function kindOf(row) {
  const m = /activity-event--(\w+)/.exec(row.className);
  return m ? m[1] : null;
}
function fireDoc(type, event) {
  for (const fn of (docListeners[type] || [])) fn(event);
}
function keyEvent(key) {
  return { key, target: { tagName: 'BODY' }, preventDefault: () => {} };
}

/* 1. One row per event, classified, all content as text (never innerHTML). */
const feed = getEl('activity-feed');
X.renderActivityEvents([
  { kind: 'text', at: '2026-01-01T00:00:00Z', text: 'I will run the tests.' },
  { kind: 'reasoning', at: '2026-01-01T00:00:01Z', text: 'the suite is slow' },
  { kind: 'tool', at: '2026-01-01T00:00:02Z', tool: 'file_system.bash', summary: 'pytest -q' },
  { kind: 'result', at: '2026-01-01T00:00:03Z', ok: true, summary: '619 passed' },
  { kind: 'status', at: '2026-01-01T00:00:04Z', text: 'Running file_system.bash' },
]);
const rows = feed.children.filter((c) => c.classList.contains('activity-event'));
check(rows.length === 5, 'one row per event, got ' + rows.length);
check(JSON.stringify(rows.map(kindOf)) === JSON.stringify(['text', 'reasoning', 'tool', 'result', 'status']),
  'rows are classified in order, got ' + JSON.stringify(rows.map(kindOf)));
check(textOf(rows[0]).indexOf('I will run the tests.') !== -1,
  'the assistant text is shown, got ' + JSON.stringify(textOf(rows[0])));
check(textOf(rows[2]).indexOf('file_system.bash') !== -1,
  'the tool row names the tool, got ' + JSON.stringify(textOf(rows[2])));
check(textOf(rows[2]).indexOf('pytest -q') !== -1,
  'the tool row shows the call summary, got ' + JSON.stringify(textOf(rows[2])));
check(textOf(rows[3]).indexOf('619 passed') !== -1,
  'the result row shows the output, got ' + JSON.stringify(textOf(rows[3])));
check(rows[3].classList.contains('activity-event--ok'),
  'a successful result is marked ok, got ' + rows[3].className);
check(textOf(rows[4]).indexOf('Running file_system.bash') !== -1,
  'the status row shows the status text, got ' + JSON.stringify(textOf(rows[4])));
let htmlUsed = false;
walk(feed, (n) => { if (n._html !== null) htmlUsed = true; });
check(!htmlUsed, 'the activity feed never uses innerHTML (journal text is model-written)');

/* 2. Reasoning is collapsed behind a "thinking…" toggle. */
const reasoning = rows[1];
const toggle = reasoning.children.find((c) => c.classList.contains('activity-thinking'));
check(!!toggle, 'the reasoning row has a thinking toggle');
check(/thinking/i.test(textOf(toggle)), 'the toggle says "thinking", got ' + JSON.stringify(textOf(toggle)));
const thinking = reasoning.children.find((c) => c.classList.contains('activity-text'));
check(!!thinking, 'the reasoning row carries its text');
check(thinking.hidden === true, 'reasoning is collapsed by default');
check(toggle.getAttribute('aria-expanded') === 'false', 'the collapsed toggle is aria-expanded=false');
toggle.listeners.click[0]();
check(thinking.hidden === false, 'clicking the toggle reveals the thinking');
check(toggle.getAttribute('aria-expanded') === 'true', 'the open toggle is aria-expanded=true');
toggle.listeners.click[0]();
check(thinking.hidden === true, 'clicking again collapses it');

/* 3. A failed result reads as failed. */
X.renderActivityEvents([{ kind: 'result', at: '2026-01-01T00:00:05Z', ok: false, summary: 'boom' }]);
const failed = feed.children[feed.children.length - 1];
check(failed.classList.contains('activity-event--failed'),
  'a failed result is marked failed, got ' + failed.className);

/* 4. The popup is a small floating panel, positioned near the card and
 *    clamped to the viewport — never a full-screen overlay. */
const popup = getEl('activity-popup');
const anchor = makeEl('article');
anchor.getBoundingClientRect = () => ({ left: 900, top: 300, width: 200, height: 140, right: 1100, bottom: 440 });
setBoard([{ id: 'card-1', title: 'Live card', status: 'running', session: { live_tokens: 1234 } }]);
activityResponse = { cursor: 0, events: [] };
X.openActivity('card-1', anchor);
check(popup.hidden === false, 'the popup is shown');
check(!popup.classList.contains('overlay'), 'the popup is not a full-screen overlay');
check(X.ACTIVITY_WIDTH === 420 && X.ACTIVITY_HEIGHT === 520,
  'the popup is ~420x520, got ' + X.ACTIVITY_WIDTH + 'x' + X.ACTIVITY_HEIGHT);
const left = parseFloat(popup.style.left);
const top = parseFloat(popup.style.top);
check(left >= 0 && left + X.ACTIVITY_WIDTH <= 1280,
  'the popup is clamped horizontally, got left=' + left);
check(top >= 0 && top + X.ACTIVITY_HEIGHT <= 800,
  'the popup is clamped vertically, got top=' + top);
check(left < 900, 'a card near the right edge opens the popup to its left, got left=' + left);
check(textOf(getEl('activity-title')).indexOf('Live card') !== -1,
  'the header shows the card title, got ' + JSON.stringify(textOf(getEl('activity-title'))));
check(textOf(getEl('activity-status')).indexOf('running') !== -1,
  'the header shows the status pill, got ' + JSON.stringify(textOf(getEl('activity-status'))));
check(textOf(getEl('activity-tokens')).indexOf('1.2k') !== -1,
  'the header shows the live token count, got ' + JSON.stringify(textOf(getEl('activity-tokens'))));

/* 5. Auto-scroll: at the bottom it follows new events; scrolled up it stops
 *    and offers "jump to latest". */
feed.scrollHeight = 500; feed.clientHeight = 100; feed.scrollTop = 400;
X.scrollActivityToLatest();
check(feed.scrollTop === feed.scrollHeight, 'jump to latest scrolls to the bottom');
feed.scrollTop = 0;  // the user scrolled up
for (const fn of (feed.listeners.scroll || [])) fn({});
check(X.state.activityAutoScroll === false, 'scrolling up stops the auto-scroll');
check(getEl('activity-jump').hidden === false, 'scrolling up offers "jump to latest"');
feed.scrollTop = 400;  // back at the bottom
for (const fn of (feed.listeners.scroll || [])) fn({});
check(X.state.activityAutoScroll === true, 'scrolling back to the bottom resumes the auto-scroll');
check(getEl('activity-jump').hidden === true, 'the jump affordance hides at the bottom');

/* 6. The gesture: C + click opens the popup, a plain click opens the editor. */
const card = X.buildCard({ id: 'card-1', title: 'Live card', body: 'spec', status: 'running',
                           session: { started_at: '2026-01-01T00:00:00Z' }, questions: [] });
X.closeActivity();
X.state.editorCardId = null;
card.listeners.click[0]({});
check(X.state.editorCardId === 'card-1', 'a plain click opens the editor');
check(X.state.activityCardId === null, 'a plain click does not open the popup');
X.closeEditor();
fireDoc('keydown', keyEvent('c'));
card.listeners.click[0]({});
check(X.state.activityCardId === 'card-1', 'C + click opens the activity popup');
check(X.state.editorCardId === null, 'C + click does not open the editor');
fireDoc('keyup', keyEvent('c'));
X.closeActivity();
card.listeners.click[0]({});
check(X.state.editorCardId === 'card-1', 'releasing C restores the plain click');
X.closeEditor();

/* 7. Esc closes the popup. */
X.openActivity('card-1', anchor);
check(popup.hidden === false, 'the popup is open before Esc');
fireDoc('keydown', keyEvent('Escape'));
check(popup.hidden === true, 'Esc closes the popup');
check(X.state.activityCardId === null, 'Esc clears the popup card');

/* 7a. The × button closes it too. */
X.openActivity('card-1', anchor);
check(popup.hidden === false, 'the popup is open before the × click');
getEl('activity-close').listeners.click[0]();
check(popup.hidden === true, 'the × closes the popup');
check(X.state.activityCardId === null, 'the × clears the popup card');

/* 7b. A modal on top takes Esc first; the popup closes after it. */
X.openActivity('card-1', anchor);
getEl('editor-overlay').hidden = false;
fireDoc('keydown', keyEvent('Escape'));
check(getEl('editor-overlay').hidden === true, 'Esc closes the modal on top first');
check(popup.hidden === false, 'the popup stays open while a modal is on top');
fireDoc('keydown', keyEvent('Escape'));
check(popup.hidden === true, 'the next Esc closes the popup');

/* 8. Polling: new events are appended, and it stops when the card is no
 *    longer running/reviewing. */
setBoard([{ id: 'card-1', title: 'Live card', status: 'running', session: {} }]);
activityResponse = { cursor: 2, events: [
  { kind: 'text', at: '2026-01-01T00:00:00Z', text: 'first' },
  { kind: 'tool', at: '2026-01-01T00:00:01Z', tool: 'file_system.bash', summary: 'pytest -q' },
] };
X.openActivity('card-1', anchor);
await X.pollActivity();
check(X.state.activityCursor === 2, 'the cursor follows the server, got ' + X.state.activityCursor);
check(feed.children.filter((c) => c.classList.contains('activity-event')).length >= 2,
  'the polled events are rendered');
check(X.state.activityTimer !== null, 'a live card keeps polling');
const timer = X.state.activityTimer;
setBoard([{ id: 'card-1', title: 'Live card', status: 'done', session: {} }]);
await X.pollActivity();
check(X.state.activityTimer === null, 'polling stops when the card leaves running/reviewing');
check(timers.cleared.indexOf(timer) !== -1, 'the poll timer is cleared');
X.closeActivity();

/* 8b. A transient poll failure keeps retrying; a deleted card stops. */
setBoard([{ id: 'card-1', title: 'Live card', status: 'running', session: {} }]);
activityResponse = { cursor: 0, events: [] };
await X.openActivity('card-1', anchor);
check(X.state.activityTimer !== null, 'a live card polls');
activityFailure = 500;
await X.pollActivity();
check(X.state.activityTimer !== null, 'a transient poll failure keeps retrying');
activityFailure = 404;
await X.pollActivity();
check(X.state.activityTimer === null, 'a deleted card stops polling');
activityFailure = null;
X.closeActivity();

/* 9. Chat mode is gone: no state, no panel, no rail button, no CSS class. */
check(!('chatMode' in X.state), 'state.chatMode is gone');
check(!('chat' in X.SIDEBAR_PANELS), 'the chat sidebar panel is gone');
check(typeof sandbox.renderChatPanel === 'undefined', 'renderChatPanel is gone');
check(typeof sandbox.loadChatMode === 'undefined', 'loadChatMode is gone');
check(typeof sandbox.saveChatMode === 'undefined', 'saveChatMode is gone');
check(!/chatMode|renderChatPanel|chat-mode/.test(src), 'no chat-mode code remains in app.js');

/* 10. The Keys panel lists the C + click gesture. */
const keys = X.KEYBINDINGS.map((b) => b.key);
check(keys.some((k) => /^C\b/.test(k) || /C \+ click/i.test(k)),
  'KEYBINDINGS lists the C + click gesture, got ' + JSON.stringify(keys));

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);

}
main().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(2);
});
