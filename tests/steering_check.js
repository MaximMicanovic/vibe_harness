/* Steering reproducer for card f7462f: send guidance from the chat popup.
 *
 * Usage: node tests/steering_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   - the popup has a footer input + Send; sending POSTs
 *     /api/cards/<id>/messages {text} and shows the message in the feed
 *     immediately (before the next poll);
 *   - a worker reply (to="user") appears in the feed as a reply;
 *   - reviewer <-> worker traffic stays out of the conversation;
 *   - while the card is not live the input is disabled with the hint
 *     "this card is not running";
 *   - every message is a TEXT NODE (user- and model-written — never innerHTML);
 *   - the poll picks up messages that arrive between sends.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: steering_check.js <app.js>'); process.exit(2); }

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
    _html: null, value: '', disabled: false,
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
let messagesResponse = { messages: [] };
let boardCards = [];
const requests = [];   // {url, method, body}

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
  fetch: async (url, options) => {
    const path = String(url).split('?')[0];
    const method = (options && options.method) || 'GET';
    requests.push({ url: String(url), method,
                    body: options && options.body ? JSON.parse(options.body) : null });
    let body = {};
    if (path.endsWith('/activity')) body = activityResponse;
    else if (path.endsWith('/messages')) {
      if (method === 'POST') {
        const text = JSON.parse(options.body).text;
        const msg = { seq: messagesResponse.messages.length + 1, from: 'user',
                      to: 'worker', text, ts: '2026-01-01T00:00:00Z' };
        messagesResponse.messages.push(msg);
        body = msg;
      } else {
        body = messagesResponse;
      }
    } else if (path === '/api/cards') body = boardCards;
    else if (path === '/api/stacks' || path === '/api/projects'
             || path === '/api/templates' || path === '/api/archives') body = [];
    return { ok: true, status: 200, statusText: 'OK',
             json: async () => body, text: async () => '' };
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
vm.runInContext(src + '\n;globalThis.__x = { state, openActivity, closeActivity, '
  + 'pollActivity, sendActivityMessage, renderActivityMessages, '
  + 'activityMessageRow, updateActivityCompose, activityIsLive };', sandbox,
  { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.sendActivityMessage !== 'function') {
  console.error('could not load sendActivityMessage from app.js');
  process.exit(2);
}

function setBoard(cards) {
  boardCards.length = 0;
  boardCards.push(...cards);
  X.state.cards = boardCards;
}

for (const id of ['editor-overlay', 'log-overlay', 'diff-overlay', 'projects-overlay']) {
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
function bubbles() {
  return getEl('activity-feed').children.filter((c) => c.classList.contains('activity-message'));
}

const anchor = makeEl('article');
const input = getEl('activity-input');
const send = getEl('activity-send');
const hint = getEl('activity-hint');
const compose = getEl('activity-compose');

/* 1. A live card: the footer is usable, no hint. */
setBoard([{ id: 'card-1', title: 'Live card', status: 'running', session: {} }]);
await X.openActivity('card-1', anchor);
check(X.activityIsLive() === true, 'the card is live');
check(input.disabled === false, 'the input is enabled while the card runs');
check(send.disabled === false, 'Send is enabled while the card runs');
check(hint.hidden === true, 'the "not running" hint is hidden while the card runs');

/* 2. Sending posts the message and shows it immediately. */
input.value = 'focus on the tests first';
const sent = await X.sendActivityMessage();
check(!!sent && sent.text === 'focus on the tests first',
  'sendActivityMessage returns the stored message, got ' + JSON.stringify(sent));
const post = requests.filter((r) => r.method === 'POST' && /\/messages$/.test(r.url)).pop();
check(!!post, 'sending POSTs to /api/cards/<id>/messages');
check(post && post.url === '/api/cards/card-1/messages',
  'the POST targets the open card, got ' + (post && post.url));
check(post && post.body && post.body.text === 'focus on the tests first',
  'the POST body carries the text, got ' + JSON.stringify(post && post.body));
check(input.value === '', 'the input is cleared after sending');
const mine = bubbles();
check(mine.length === 1, 'the sent message is in the feed immediately, got ' + mine.length);
check(mine[0] && mine[0].classList.contains('activity-message--user'),
  'the sent message is a user bubble, got ' + (mine[0] && mine[0].className));
check(textOf(mine[0]).indexOf('focus on the tests first') !== -1,
  'the bubble shows the text, got ' + JSON.stringify(textOf(mine[0])));

/* 3. A worker reply (to="user") appears as a reply. */
messagesResponse.messages.push({ seq: 2, from: 'worker', to: 'user',
                                 text: 'done: tests first', ts: '2026-01-01T00:00:01Z' });
await X.pollActivity();
const all = bubbles();
check(all.length === 2, 'the worker reply is appended by the poll, got ' + all.length);
check(all[1] && all[1].classList.contains('activity-message--worker'),
  'the reply is a worker bubble, got ' + (all[1] && all[1].className));
check(textOf(all[1]).indexOf('done: tests first') !== -1,
  'the reply shows the worker text, got ' + JSON.stringify(textOf(all[1])));

/* 4. Reviewer <-> worker traffic is not the user's conversation. */
messagesResponse.messages.push({ seq: 3, from: 'reviewer', to: 'worker',
                                 text: 'why this change?', ts: '2026-01-01T00:00:02Z' });
await X.pollActivity();
check(bubbles().length === 2, 'internal reviewer traffic stays out of the feed');

/* 5. No duplicate bubbles: the poll re-reads the whole conversation. */
await X.pollActivity();
check(bubbles().length === 2, 'a second poll does not duplicate messages');

/* 6. Every message is a TEXT NODE (never innerHTML). */
let htmlUsed = false;
walk(getEl('activity-feed'), (n) => { if (n._html !== null) htmlUsed = true; });
check(!htmlUsed, 'the conversation never uses innerHTML');

/* 7. A card that is not live: input disabled, hint shown. */
setBoard([{ id: 'card-1', title: 'Live card', status: 'done', session: {} }]);
await X.pollActivity();
check(X.activityIsLive() === false, 'the card is no longer live');
check(input.disabled === true, 'the input is disabled when the card is not running');
check(send.disabled === true, 'Send is disabled when the card is not running');
check(hint.hidden === false, 'the hint is shown when the card is not running');
check(/not running/i.test(input.placeholder),
  'the input says the card is not running, got ' + JSON.stringify(input.placeholder));

/* 8. Sending while not live is a no-op (no POST, no bubble). */
const before = requests.length;
input.value = 'too late';
const late = await X.sendActivityMessage();
check(late === null, 'sending to a card that is not running does nothing');
check(requests.length === before, 'no request is made for a card that is not running');
check(bubbles().length === 2, 'no bubble is added for a card that is not running');

/* 9. The form's submit handler sends (Enter in the input submits the form). */
setBoard([{ id: 'card-1', title: 'Live card', status: 'running', session: {} }]);
await X.pollActivity();
input.value = 'one more thing';
let prevented = false;
for (const fn of (compose.listeners.submit || [])) fn({ preventDefault: () => { prevented = true; } });
await new Promise((r) => setTimeout(r, 0));
check(prevented, 'the submit handler prevents the page reload');
check(bubbles().length === 3, 'submitting the form sends the message, got ' + bubbles().length);

/* 10. Opening another card starts a fresh conversation. */
messagesResponse = { messages: [] };
setBoard([{ id: 'card-2', title: 'Other card', status: 'running', session: {} }]);
await X.openActivity('card-2', anchor);
check(bubbles().length === 0, 'opening another card clears the conversation');
check(X.state.activityMessageSeq === 0, 'the message cursor resets per card');

const result = { ok: failures.length === 0, failures };
console.log(JSON.stringify(result, null, 2));
process.exit(failures.length ? 1 : 0);

}
main().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(2);
});
