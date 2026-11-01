/* Desktop-notification reproducer for card 66effc.
 *
 * Usage: node tests/notifications_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub, then checks the spec:
 *
 *   "Tell me when a card finishes while I am in another window."
 *
 * Concretely:
 *   - the previous status map is kept in `state` and diffed on every refresh;
 *   - a card moving from running/reviewing to done/failed/stopped raises one
 *     desktop notification naming the card and its outcome;
 *   - the first load (no previous state) and the card whose panel is open stay
 *     silent, and so does every transition that is not a finish;
 *   - permission is requested on the first click, never on load; when it is
 *     denied the fallback is a synthesised WebAudio beep (no audio asset);
 *   - the document title carries "(N) vibe-harness" while notifications are
 *     unread and the tab is hidden, and is cleared when the tab is focused;
 *   - the sidebar Settings toggle turns the feature off and persists the
 *     choice in localStorage (so it survives a reload).
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: notifications_check.js <app.js>'); process.exit(2); }

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
    offsetHeight: 140, scrollHeight: 20, clientHeight: 20,
    scrollTop: 0, scrollWidth: 0,
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
  node.focus = () => {};
  return node;
}

/* ---------- boot one app.js instance ---------- */
function boot(store) {
  const byId = {};
  const getEl = (id) => { if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; } return byId[id]; };

  const notifications = [];    // every `new Notification(...)`
  const beeps = [];            // every synthesised tone
  const permissionCalls = [];  // every Notification.requestPermission()
  const docListeners = {};
  const perm = { value: 'default' };
  const board = { cards: [] };

  const NotificationCtor = function (title, options) {
    this.title = title;
    this.body = (options && options.body) || '';
    this.tag = (options && options.tag) || '';
    this.close = () => {};
    notifications.push(this);
  };
  Object.defineProperty(NotificationCtor, 'permission', { get: () => perm.value });
  NotificationCtor.requestPermission = () => { permissionCalls.push(1); return Promise.resolve(perm.value); };

  function AudioContextStub() {
    this.currentTime = 0;
    this.destination = {};
    this.createOscillator = () => ({
      type: '', frequency: { value: 0 },
      connect: () => {}, start: () => beeps.push({ at: 'start' }), stop: () => {},
    });
    this.createGain = () => ({ gain: { value: 0 }, connect: () => {} });
    this.close = () => {};
  }

  const doc = {
    getElementById: getEl,
    createElement: (t) => makeEl(t),
    createElementNS: (ns, t) => makeEl(t),
    createTextNode: (t) => { const n = makeEl('#text'); n.textContent = t; return n; },
    addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
    removeEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    body: makeEl('body'),
    title: 'Vibe Harness',
    hidden: false,
    visibilityState: 'visible',
  };

  const sandbox = {
    console,
    document: doc,
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    fetch: async (url) => {
      const u = String(url);
      let data = {};
      if (u.indexOf('/api/cards') === 0) data = board.cards;
      else if (u.indexOf('/api/stats') === 0) {
        data = { counts: {}, total_tokens: 0, live_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0, project: null };
      } else if (u.indexOf('/api/stacks') === 0) data = [];
      return { ok: true, status: 200, statusText: 'OK', json: async () => data, text: async () => '' };
    },
    requestAnimationFrame: (fn) => fn(),
    getComputedStyle: () => ({ fontSize: '15px' }),
    CSS: { escape: (s) => String(s) },
    navigator: { platform: 'test', userAgentData: null },
    location: { reload: () => {} },
    Notification: NotificationCtor,
    AudioContext: AudioContextStub,
    setInterval: () => 0, clearInterval: () => {},
    setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; }, clearTimeout: () => {},
    Date, Math, JSON, Promise, Set, Map, Array, Object, String, Number, Boolean,
    parseFloat, parseInt, isNaN, RegExp, Error,
  };
  sandbox.addEventListener = () => {};
  sandbox.focus = () => {};
  sandbox.confirm = () => true;
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);

  const src = fs.readFileSync(APP_JS, 'utf8');
  vm.runInContext(src + '\n;globalThis.__x = { state, refresh, renderSettingsPanel,'
    + ' setNotificationsEnabled, loadNotificationsEnabled, updateTitleBadge,'
    + ' askNotificationPermission, diffCardStatuses, announceFinished, notifyCard, beep };',
    sandbox, { filename: 'app.js' });
  const X = sandbox.__x;
  if (!X || typeof X.refresh !== 'function' || typeof X.state !== 'object') {
    console.error('could not load refresh/state from app.js');
    process.exit(2);
  }

  return {
    X, doc, notifications, beeps, permissionCalls, perm, board, store, byId,
    setCards: (list) => { board.cards = list; },
    fireDoc: (type, ev) => { for (const fn of (docListeners[type] || [])) fn(ev || {}); },
  };
}

/* Find a node by id anywhere under `root` (the stub's querySelector only
 * understands class/tag selectors). */
function findById(root, id) {
  if (!root) return null;
  if (root.id === id) return root;
  for (const child of (root.children || [])) {
    const hit = findById(child, id);
    if (hit) return hit;
  }
  return null;
}

/* Let the boot-time refresh (and anything it chains) settle. */
async function flush() { for (let i = 0; i < 25; i++) await Promise.resolve(); }

function card(over) {
  return Object.assign({
    id: 'c1', title: 'a card', body: 'body', status: 'draft',
    created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T11:00:00Z',
    project: '', template: null, auto_run: false, auto_accept: false,
    depends_on: [], session: null, questions: [],
  }, over);
}

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
const textOf = (n) => ((n && n.title) || '') + ' ' + ((n && n.body) || '');

async function main() {
  const store = {};
  const app = boot(store);
  const { X, doc, notifications, beeps, permissionCalls, perm } = app;
  perm.value = 'granted';   // the common case: the user already allowed them
  await flush();
  notifications.length = 0;
  beeps.length = 0;
  permissionCalls.length = 0;

  /* 1. First load: there is no previous state, so nothing is announced. */
  X.state.prevStatuses = null;
  app.setCards([card({ id: 'c1', title: 'Fix the login bug', status: 'running' })]);
  await X.refresh();
  check(notifications.length === 0,
    'the first load must not raise a notification, got ' + notifications.length);

  /* 2. running -> done with the tab in the background: one notification
   *    naming the card and the outcome. */
  doc.hidden = true;
  doc.visibilityState = 'hidden';
  app.setCards([card({ id: 'c1', title: 'Fix the login bug', status: 'done' })]);
  await X.refresh();
  check(notifications.length === 1,
    'running -> done should raise exactly one notification, got ' + notifications.length);
  const first = notifications[0];
  check(textOf(first).indexOf('Fix the login bug') !== -1,
    'the notification must name the card, got ' + JSON.stringify(textOf(first)));
  check(/\bdone\b/.test(textOf(first)),
    'the notification must name the outcome (done), got ' + JSON.stringify(textOf(first)));
  doc.hidden = false;
  doc.visibilityState = 'visible';
  app.fireDoc('visibilitychange', {});

  /* 3. reviewing -> failed and running -> stopped announce too. */
  notifications.length = 0;
  app.setCards([card({ id: 'c2', title: 'Ship the parser', status: 'reviewing' })]);
  await X.refresh();
  app.setCards([card({ id: 'c2', title: 'Ship the parser', status: 'failed' })]);
  await X.refresh();
  check(notifications.length === 1 && /\bfailed\b/.test(textOf(notifications[0])),
    'reviewing -> failed should announce "failed", got ' + JSON.stringify(notifications.map(textOf)));

  notifications.length = 0;
  app.setCards([card({ id: 'c3', title: 'Long job', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c3', title: 'Long job', status: 'stopped' })]);
  await X.refresh();
  check(notifications.length === 1 && /\bstopped\b/.test(textOf(notifications[0])),
    'running -> stopped should announce "stopped", got ' + JSON.stringify(notifications.map(textOf)));

  /* 4. Transitions that are not a finish stay silent. */
  notifications.length = 0;
  app.setCards([card({ id: 'c4', title: 'Quiet', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c4', title: 'Quiet', status: 'reviewing' })]);
  await X.refresh();
  check(notifications.length === 0,
    'running -> reviewing must stay silent, got ' + notifications.length);
  app.setCards([card({ id: 'c4', title: 'Quiet', status: 'done' })]);
  await X.refresh();
  check(notifications.length === 1,
    'reviewing -> done should announce once, got ' + notifications.length);
  notifications.length = 0;
  app.setCards([card({ id: 'c4', title: 'Quiet', status: 'accepted' })]);
  await X.refresh();
  check(notifications.length === 0,
    'done -> accepted must stay silent, got ' + notifications.length);

  /* 5. The card whose panel is open never notifies. */
  notifications.length = 0;
  X.state.editorCardId = 'c5';
  app.setCards([card({ id: 'c5', title: 'Open card', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c5', title: 'Open card', status: 'done' })]);
  await X.refresh();
  check(notifications.length === 0,
    'the card whose panel is open must not notify, got ' + notifications.length);
  X.state.editorCardId = null;

  /* ...and neither does the card whose log panel is open. */
  notifications.length = 0;
  X.state.logCardId = 'c5';
  app.setCards([card({ id: 'c5', title: 'Open card', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c5', title: 'Open card', status: 'failed' })]);
  await X.refresh();
  check(notifications.length === 0,
    'the card whose log panel is open must not notify, got ' + notifications.length);
  X.state.logCardId = null;

  /* 6. Permission denied -> a synthesised beep instead of a notification. */
  perm.value = 'denied';
  notifications.length = 0;
  beeps.length = 0;
  app.setCards([card({ id: 'c6', title: 'Beep card', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c6', title: 'Beep card', status: 'done' })]);
  await X.refresh();
  check(notifications.length === 0,
    'denied permission must not construct a Notification, got ' + notifications.length);
  check(beeps.length === 1,
    'denied permission must fall back to one synthesised beep, got ' + beeps.length);
  perm.value = 'granted';

  /* 7. Permission is asked on the first click, never on load. A fresh boot
   *    starts with permission still undecided ("default"). */
  const fresh = boot({});
  await flush();
  check(fresh.permissionCalls.length === 0,
    'permission must not be requested on load, got ' + fresh.permissionCalls.length);
  fresh.fireDoc('click', {});
  check(fresh.permissionCalls.length === 1,
    'the first click must request notification permission, got ' + fresh.permissionCalls.length);
  fresh.fireDoc('click', {});
  check(fresh.permissionCalls.length === 1,
    'permission is requested once, got ' + fresh.permissionCalls.length);

  /* 8. Title badge while the tab is hidden, cleared on focus. */
  X.state.unreadNotifications = 0;
  doc.hidden = true;
  doc.visibilityState = 'hidden';
  app.setCards([card({ id: 'c7', title: 'Hidden card', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c7', title: 'Hidden card', status: 'done' })]);
  await X.refresh();
  check(doc.title === '(1) vibe-harness',
    'hidden + one unread -> "(1) vibe-harness", got ' + JSON.stringify(doc.title));
  app.setCards([card({ id: 'c8', title: 'Another', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c8', title: 'Another', status: 'failed' })]);
  await X.refresh();
  check(doc.title === '(2) vibe-harness',
    'hidden + two unread -> "(2) vibe-harness", got ' + JSON.stringify(doc.title));
  doc.hidden = false;
  doc.visibilityState = 'visible';
  app.fireDoc('visibilitychange', {});
  check(doc.title === 'Vibe Harness',
    'focusing the tab clears the badge, got ' + JSON.stringify(doc.title));
  check(X.state.unreadNotifications === 0,
    'focusing the tab clears the unread count, got ' + X.state.unreadNotifications);

  /* 9. The sidebar Settings toggle turns the feature off and persists it. */
  await X.renderSettingsPanel();
  const toggle = findById(app.byId['sidebar-body'], 'notify-toggle');
  check(!!toggle, 'the Settings panel must render a notifications toggle');
  if (toggle) {
    check(toggle.checked === true, 'the toggle starts on (notifications default to on)');
    toggle.checked = false;
    for (const fn of (toggle.listeners.change || [])) fn({ target: toggle });
  }
  check(store['vibe-harness:notifications'] === '0',
    'the toggle must persist "off" in localStorage, got ' + JSON.stringify(store['vibe-harness:notifications']));
  check(X.loadNotificationsEnabled() === false,
    'a reload must read the persisted "off" back');
  notifications.length = 0;
  beeps.length = 0;
  doc.hidden = true;
  app.setCards([card({ id: 'c9', title: 'Muted', status: 'running' })]);
  await X.refresh();
  app.setCards([card({ id: 'c9', title: 'Muted', status: 'done' })]);
  await X.refresh();
  check(notifications.length === 0 && beeps.length === 0,
    'notifications off -> nothing fires, got ' + notifications.length + ' notifications / ' + beeps.length + ' beeps');
  check(doc.title === 'Vibe Harness',
    'notifications off -> no title badge, got ' + JSON.stringify(doc.title));

  /* 10. A fresh boot with the same localStorage starts switched off. */
  const reloaded = boot(store);
  await flush();
  check(reloaded.X.state.notificationsEnabled === false,
    'the toggle must survive a reload, got ' + JSON.stringify(reloaded.X.state.notificationsEnabled));

  const result = { ok: failures.length === 0, failures };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(2);
});
