/* Queue chip + Settings panel check for card 03f38a (concurrency limit).
 *
 * Usage: node tests/queue_chip_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and checks the spec:
 *
 *   - a card queued for CAPACITY shows the server's reason
 *     ("waiting for a free slot (2/3 running)") in a neutral chip — it is a
 *     normal wait, not an error (no red merge-conflict chip);
 *   - a card queued behind an in-progress MERGE CONFLICT keeps its red chip;
 *   - the sidebar's Settings panel renders a "Max concurrent sessions" number
 *     input (0 = unlimited) plus the current occupancy ("2 of 3 running"), and
 *     changing the input PUTs {"max_concurrent": N} to /api/settings.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: queue_chip_check.js <app.js>'); process.exit(2); }

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
vm.runInContext(src + '\n;globalThis.__x = { state, buildCard, renderSettingsPanel };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.buildCard !== 'function' || typeof X.renderSettingsPanel !== 'function') {
  console.error('could not load buildCard/renderSettingsPanel from app.js');
  process.exit(2);
}

/* ---------- checks ---------- */
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

function findById(node, id) {
  if (node.id === id) return node;
  for (const c of node.children) {
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}

function note(over) {
  return Object.assign({
    id: 'n1', title: 'a note', body: 'body', status: 'draft',
    created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T11:00:00Z',
    project: '', template: null, auto_run: false, auto_accept: false,
    depends_on: [], session: null, questions: [],
  }, over);
}

/* 1. Capacity queue: the server's reason, in a neutral (non-error) chip. */
const capacity = X.buildCard(note({
  id: 'cap', status: 'draft',
  session: { queued: true, queue_reason: 'waiting for a free slot (2/3 running)' },
}));
const capChip = capacity.querySelector('.chip-waiting');
check(!!capChip, 'a capacity-queued card should show a neutral .chip-waiting chip');
if (capChip) {
  check(capChip.textContent.indexOf('waiting for a free slot (2/3 running)') !== -1,
    'the capacity chip should carry the server reason, got ' + JSON.stringify(capChip.textContent));
  check((capChip.title || '').indexOf('waiting for a free slot (2/3 running)') !== -1,
    'the capacity chip hover title should carry the reason, got ' + JSON.stringify(capChip.title));
}
check(capacity.querySelectorAll('.chip-merge-conflict').length === 0,
  'a capacity wait is not a merge conflict — no red .chip-merge-conflict chip');
check(capacity.className.indexOf('card--draft') !== -1,
  'a capacity-queued card stays a draft card, got ' + JSON.stringify(capacity.className));
check(capacity.className.indexOf('error') === -1 && capacity.className.indexOf('failed') === -1,
  'a capacity-queued card must not be styled as an error, got ' + JSON.stringify(capacity.className));

/* 2. Merge-conflict queue keeps its red chip. */
const conflict = X.buildCard(note({
  id: 'conf', status: 'draft',
  session: { queued: true, queue_reason: 'waiting for the in-progress merge conflict to resolve' },
}));
const conflictChip = conflict.querySelector('.chip-merge-conflict');
check(!!conflictChip, 'a merge-conflict-queued card keeps its .chip-merge-conflict chip');
if (conflictChip) {
  check(conflictChip.textContent.toLowerCase().indexOf('merge conflict') !== -1,
    'the merge-conflict chip should mention the merge conflict, got ' + JSON.stringify(conflictChip.textContent));
}
check(conflict.querySelectorAll('.chip-waiting').length === 0,
  'a merge-conflict wait is not a capacity wait — no neutral .chip-waiting chip');

/* 3. Settings panel: number input + occupancy, and the PUT on change. */
let settingsPayload = { backend: 'vibe', backends: [], max_concurrent: 3, running: 2 };
const calls = [];
sandbox.fetch = async (url, options) => {
  calls.push({ url, options });
  if (url === '/api/settings' && options && options.method === 'PUT') {
    settingsPayload = Object.assign({}, settingsPayload, JSON.parse(options.body));
  }
  const payload = url === '/api/settings' ? settingsPayload : {};
  return { ok: true, status: 200, statusText: 'OK', json: async () => payload };
};

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

(async () => {
  await X.renderSettingsPanel();
  const body = getEl('sidebar-body');
  const input = findById(body, 'max-concurrent-input');
  check(!!input, 'the Settings panel should render a #max-concurrent-input');
  if (input) {
    check(input.type === 'number', 'the max-concurrent input should be a number input, got ' + JSON.stringify(input.type));
    check(String(input.value) === '3', 'the input should show the current cap (3), got ' + JSON.stringify(input.value));
    check(String(input.min) === '0', 'the input should allow 0 (unlimited), got min=' + JSON.stringify(input.min));
  }
  const occupancy = findById(body, 'max-concurrent-occupancy');
  check(!!occupancy, 'the Settings panel should render the occupancy line');
  if (occupancy) {
    check(occupancy.textContent === '2 of 3 running',
      'occupancy should read "2 of 3 running", got ' + JSON.stringify(occupancy.textContent));
  }

  if (input && input.listeners.change && input.listeners.change.length) {
    input.value = '1';
    input.listeners.change[0]();
    await flush();
    const put = calls.filter((c) => c.url === '/api/settings' && c.options && c.options.method === 'PUT').pop();
    check(!!put, 'changing the input should PUT /api/settings');
    if (put) {
      check(JSON.parse(put.options.body).max_concurrent === 1,
        'the PUT body should carry max_concurrent: 1, got ' + put.options.body);
    }
  } else {
    failures.push('the max-concurrent input should save on change');
  }

  /* 4. Unlimited (0): the occupancy line says so instead of "2 of 0 running". */
  settingsPayload = { backend: 'vibe', backends: [], max_concurrent: 0, running: 2 };
  await X.renderSettingsPanel();
  const unlimited = findById(getEl('sidebar-body'), 'max-concurrent-occupancy');
  check(!!unlimited && unlimited.textContent.indexOf('2') !== -1
        && unlimited.textContent.toLowerCase().indexOf('no limit') !== -1,
    'with 0 (unlimited) the occupancy should read "2 running (no limit)", got '
      + JSON.stringify(unlimited && unlimited.textContent));

  const result = { ok: failures.length === 0, failures };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
})();
