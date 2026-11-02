/* Editor conflict reproducer: two people editing the same card.
 *
 * Usage: node tests/editor_conflict_check.js <path-to-app.js>
 *
 * Boots the served app.js under a minimal DOM stub and drives the editor:
 *
 *   - openEditor remembers the updated_at it loaded (state.editorUpdatedAt);
 *   - saveCard sends it as expected_updated_at;
 *   - a 409 keeps the editor open with the user's text on screen and shows the
 *     conflict box ("this card changed since you opened it");
 *   - "overwrite anyway" re-sends WITHOUT expected_updated_at and succeeds;
 *   - "reload theirs" replaces the fields with the other person's text.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node editor_conflict_check.js <app.js>'); process.exit(2); }

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
    scrollTop: 0, scrollWidth: 0, value: '',
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

/* ---------- scripted server ---------- */
const T1 = '2026-11-02T10:00:00+00:00';   // the version the editor opened
const T2 = '2026-11-02T10:05:00+00:00';   // the other person's save
let card = {
  id: 'c1', title: 'Card', body: 'theirs', status: 'draft',
  created_at: '2026-11-02T09:00:00+00:00', updated_at: T1,
  template: null, auto_run: false, auto_accept: false, project: '',
  workdir: null, depends_on: [], parent_id: null, model: null,
  session: null, questions: [], archived: false,
};
let puts = [];          // every PUT payload the editor sent
let conflict = false;   // the server refuses stale saves
const jsonRes = (data, status) => ({
  ok: (status || 200) < 400, status: status || 200,
  statusText: (status || 200) === 409 ? 'Conflict' : 'OK',
  headers: { get: () => null },
  json: async () => data,
  text: async () => JSON.stringify(data),
});
const fetchStub = async (path, opts) => {
  const method = (opts && opts.method) || 'GET';
  if (path === '/api/cards/c1' && method === 'GET') return jsonRes(card);
  if (path === '/api/cards/c1' && method === 'PUT') {
    const payload = JSON.parse(opts.body);
    puts.push(payload);
    if (conflict && payload.expected_updated_at !== undefined) {
      return jsonRes({ detail: 'this card changed since you opened it' }, 409);
    }
    card = Object.assign({}, card, {
      title: payload.title, body: payload.body, updated_at: T2,
    });
    return jsonRes(card);
  }
  if (path === '/api/cards') return jsonRes([card]);
  if (path.startsWith('/api/stats')) return jsonRes({});
  if (path === '/api/stacks') return jsonRes([]);
  if (path === '/api/projects') return jsonRes([]);
  if (path === '/api/models') return jsonRes({ roles: {}, models: [], default_model: null });
  if (path === '/api/settings') return jsonRes({ backend: 'vibe', backends: [] });
  if (path === '/api/version') return jsonRes({ version: 'v1' });
  return jsonRes({});
};

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
  fetch: fetchStub,
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
vm.runInContext(src + '\n;globalThis.__x = { state, openEditor, saveCard };', sandbox, { filename: 'app.js' });
const X = sandbox.__x;
if (!X || typeof X.openEditor !== 'function' || typeof X.saveCard !== 'function') {
  console.error('could not load openEditor/saveCard from app.js');
  process.exit(2);
}

/* ---------- checks ---------- */
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
const click = async (id) => {
  const el = getEl(id);
  const fn = (el.listeners.click || [])[0];
  if (!fn) { failures.push('no click handler on #' + id); return; }
  await fn();
};
const lastPut = () => puts[puts.length - 1] || {};

async function main() {
  /* 1. openEditor remembers the version it loaded. */
  await X.openEditor('c1');
  check(X.state.editorUpdatedAt === T1,
    'openEditor remembers the loaded updated_at, got ' + JSON.stringify(X.state.editorUpdatedAt));
  check(getEl('card-body').value === 'theirs', 'the editor shows the loaded body');

  /* 2. A save names that version. */
  getEl('card-body').value = 'my text';
  await X.saveCard();
  check(lastPut().expected_updated_at === T1,
    'saveCard sends expected_updated_at, got ' + JSON.stringify(lastPut().expected_updated_at));
  check(lastPut().body === 'my text', 'saveCard sends the edited body');

  /* 3. A lost race: 409 keeps the text and shows the conflict. */
  card = Object.assign({}, card, { body: 'theirs', updated_at: T2 });
  await X.openEditor('c1');
  getEl('card-body').value = 'my text';
  conflict = true;
  await click('btn-save');
  check(getEl('edit-conflict').hidden === false,
    'a 409 shows the conflict box');
  check(/changed since you opened it/i.test(getEl('edit-conflict-text').textContent),
    'the conflict box names the conflict, got ' + JSON.stringify(getEl('edit-conflict-text').textContent));
  check(getEl('card-body').value === 'my text',
    'a 409 keeps the user text on screen, got ' + JSON.stringify(getEl('card-body').value));
  check(getEl('editor-overlay').hidden === false, 'a 409 keeps the editor open');
  check(lastPut().expected_updated_at === T2,
    'the refused save carried the version the editor loaded');

  /* 4. "Overwrite anyway" re-sends without expected_updated_at and wins. */
  conflict = false;
  await click('btn-overwrite');
  check(!('expected_updated_at' in lastPut()),
    'overwrite anyway sends no expected_updated_at, got ' + JSON.stringify(lastPut()));
  check(lastPut().body === 'my text', 'overwrite anyway sends the user text');
  check(card.body === 'my text', 'the card ends up with the second client text');
  check(getEl('editor-overlay').hidden === true, 'overwrite anyway closes the editor');

  /* 5. "Reload theirs" replaces the fields with the other person's text. */
  card = Object.assign({}, card, { body: 'theirs', updated_at: T1 });
  await X.openEditor('c1');
  getEl('card-body').value = 'my text';
  conflict = true;
  await click('btn-save');
  check(getEl('edit-conflict').hidden === false, 'the conflict box is shown before reloading');
  conflict = false;
  await click('btn-reload-theirs');
  check(getEl('card-body').value === 'theirs',
    'reload theirs shows the other person text, got ' + JSON.stringify(getEl('card-body').value));
  check(getEl('edit-conflict').hidden === true, 'reload theirs hides the conflict box');
  check(X.state.editorUpdatedAt === T1,
    'reload theirs remembers the version it reloaded, got ' + JSON.stringify(X.state.editorUpdatedAt));

  const result = { ok: failures.length === 0, failures };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
