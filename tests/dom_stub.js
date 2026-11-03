/* Shared minimal DOM stub for the node checks that boot the served app.js.
 *
 * The checks in this folder drive the *served* front-end under Node: they read
 * app.js, run it in a `vm` context whose `document` is this stub, and then poke
 * the exported functions. The stub only implements what app.js touches —
 * elements, class lists, listeners, a tiny selector engine, localStorage,
 * ResizeObserver and fetch — so a check can assert on what the browser would
 * show.
 *
 * Usage:
 *
 *   const { makeDom } = require('./dom_stub.js');
 *   const dom = makeDom({ fetch: myFetch });
 *   const X = dom.boot(appJsPath, ['state', 'loadProjects']);
 *   await dom.flush();                       // let the boot refresh() land
 *   dom.getEl('project-select').children;    // what the browser would show
 *
 * `fetch` defaults to an empty board (no cards, no projects, no archives), so a
 * check that only cares about one endpoint can override just that path.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

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
  // One compound selector: tag, .classes and [attr="value"] parts.
  const attrRe = /\[([\w-]+)="([^"]*)"\]/g;
  let m;
  while ((m = attrRe.exec(sel)) !== null) {
    const key = m[1].replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (n.dataset[key] !== m[2]) return false;
  }
  const bare = sel.replace(attrRe, '');
  const tagPart = bare.split('.')[0];
  if (tagPart && tagPart !== '*' && n.tagName !== tagPart.toUpperCase()) return false;
  const classes = bare.split('.').slice(1).filter(Boolean);
  return classes.every((c) => n.classList.contains(c));
}

/* Split a selector on whitespace, but not inside an attribute value
 * ("[data-where=\"Vibe Harness / Old work\"]" is one part). */
function splitSelector(sel) {
  const parts = [];
  let cur = '';
  let quote = null;
  for (const ch of sel) {
    if (quote) { cur += ch; if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
    if (/\s/.test(ch)) { if (cur) { parts.push(cur); cur = ''; } continue; }
    cur += ch;
  }
  if (cur) parts.push(cur);
  return parts;
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
    options: [],
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
    const parts = splitSelector(sel);
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (matchComplex(c, parts)) out.push(c); walk(c); } };
    walk(node);
    return out;
  };
  node.closest = (sel) => { let n = node; while (n) { if (n.classList && matchSimple(n, sel)) return n; n = n.parentNode; } return null; };
  /* The box the browser would report: the inline geometry when the app set one
   * (a floating panel's left/top/width/height), else a card-sized default. A
   * check that simulates the native resize grip sets `node._rect` — the grip
   * changes the used box without touching the inline style. */
  node.getBoundingClientRect = () => {
    if (node._rect) return node._rect;
    const px = (v, fallback) => { const n = parseFloat(v); return isFinite(n) ? n : fallback; };
    const left = px(node.style.left, 0);
    const top = px(node.style.top, 0);
    const width = px(node.style.width, 200);
    const height = px(node.style.height, 140);
    return { left, top, width, height, right: left + width, bottom: top + height };
  };
  node.focus = () => { node._focused = true; };
  node.blur = () => { node._focused = false; };
  node.scrollIntoView = () => { node._scrolled = true; };
  return node;
}

/* The elements index.html ships with the `hidden` attribute — the stub has no
 * markup, so mirror them here (an overlay that starts "visible" would swallow
 * the keyboard shortcuts). */
const HIDDEN_BY_DEFAULT = [
  'search-hint', 'search-results', 'error-banner', 'gate-banner', 'sidebar-panel',
  'empty-board', 'editor-overlay', 'log-overlay', 'diff-overlay',
  'archives-overlay', 'projects-overlay', 'depends-field', 'accept-hint',
  'btn-stop', 'btn-accept', 'btn-archive', 'btn-log',
];

/* An empty board: enough for app.js to boot when a check only cares about one
 * endpoint (pass `fetch` to answer the paths that matter). */
function emptyFetch(path) {
  const p = String(path);
  let payload = {};
  if (p === '/api/cards') payload = [];
  else if (p.startsWith('/api/projects')) payload = [];
  else if (p.startsWith('/api/stacks')) payload = [];
  else if (p.startsWith('/api/archives')) payload = [];
  else if (p.startsWith('/api/models')) payload = { roles: {}, models: [], default_model: null };
  else if (p.startsWith('/api/settings')) payload = { backend: 'vibe', backends: [] };
  else if (p.startsWith('/api/version')) payload = { version: 'test' };
  else if (p.startsWith('/api/stats')) payload = { total_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0 };
  return {
    ok: true, status: 200, statusText: 'OK',
    headers: { get: () => null },
    json: async () => payload,
    text: async () => '',
  };
}

function makeDom(options) {
  const opts = options || {};
  const byId = {};
  const roots = [];
  const getEl = (id) => {
    if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; roots.push(byId[id]); }
    return byId[id];
  };
  /* `store` lets a check boot a second instance over the same localStorage —
   * that is what a page reload looks like to the app. */
  const store = opts.store || {};
  const timers = [];
  const docListeners = {};
  const resizeObservers = [];

  /* ResizeObserver: records the callback and the observed nodes; the check
   * fires it with `fireResize(node)` to simulate the user dragging the native
   * resize grip. */
  class ResizeObserverStub {
    constructor(cb) { this.cb = cb; this.targets = []; resizeObservers.push(this); }
    observe(target) { if (this.targets.indexOf(target) === -1) this.targets.push(target); }
    unobserve(target) { this.targets = this.targets.filter((t) => t !== target); }
    disconnect() { this.targets = []; }
  }
  const fireResize = (target) => {
    for (const obs of resizeObservers) {
      if (obs.targets.indexOf(target) !== -1) obs.cb([{ target }], obs);
    }
  };

  for (const id of (opts.hidden || HIDDEN_BY_DEFAULT)) getEl(id).hidden = true;

  const sandbox = {
    console,
    document: {
      getElementById: getEl,
      createElement: (t) => makeEl(t),
      createElementNS: (ns, t) => makeEl(t),
      createTextNode: (t) => ({ textContent: t, children: [], childNodes: [], parentNode: null }),
      addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      querySelector: (sel) => sandbox.document.querySelectorAll(sel)[0] || null,
      querySelectorAll: (sel) => {
        const out = [];
        for (const root of roots) {
          if (matchComplex(root, splitSelector(sel))) out.push(root);
          for (const n of root.querySelectorAll(sel)) if (out.indexOf(n) === -1) out.push(n);
        }
        return out;
      },
      body: makeEl('body'),
    },
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    fetch: opts.fetch || emptyFetch,
    requestAnimationFrame: (fn) => fn(),
    ResizeObserver: ResizeObserverStub,
    getComputedStyle: () => ({ fontSize: '15px' }),
    CSS: { escape: (s) => String(s) },
    navigator: { platform: 'test', userAgentData: null },
    setInterval: () => 0, clearInterval: () => {},
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
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

  /* Let the pending promise chain (the boot refresh, a click handler) settle. */
  const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); };
  /* Run the timers the app scheduled (debounced search, polls). */
  const runTimers = () => { const due = timers.splice(0); for (const fn of due) if (fn) fn(); };

  /* Run the served app.js and hand back the named top-level bindings. */
  function boot(appJsPath, names) {
    const src = fs.readFileSync(appJsPath, 'utf8');
    const exportLine = '\n;globalThis.__x = {' + (names || []).join(', ') + '};';
    vm.runInContext(src + exportLine, sandbox, { filename: 'app.js' });
    return sandbox.__x;
  }

  return { sandbox, getEl, byId, store, timers, docListeners, resizeObservers, fireResize, flush, runTimers, boot };
}

module.exports = {
  makeDom, makeEl, makeClassList, matchSimple, matchComplex, splitSelector,
  HIDDEN_BY_DEFAULT,
};
