#!/usr/bin/env node
/* Reproducer for note a64cea: "When updating token count it scrolls me to the
 * top of the window."
 *
 * It runs the REAL app/static/app.js in Node against a minimal stub DOM. While
 * any note is live, the dashboard polls refresh() every 2 s — that poll is
 * what updates the token count — and renderBoard() tears down every board
 * column (wrap.textContent = '') before rebuilding the cards.
 *
 * The stub models the reported browser behavior: when a poll tears down DOM
 * that the window's scroll is anchored on, the anchor is lost and the window
 * scroll falls back to the top (window.scrollY = 0). That is the bug symptom.
 *
 * Scenario: boot the app, scroll the window down to y=800, then run one poll
 * (refresh()) whose response carries an updated token count. If the window
 * ends up back at the top, the bug is reproduced (exit 1); if the scroll
 * position survives the poll, exit 0.
 *
 * Exit codes: 0 = scroll preserved (fixed), 1 = bug reproduced (scroll reset
 * to the top), 2 = harness/setup error.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const APP_JS = path.join(__dirname, '..', 'app', 'static', 'app.js');

/* ---------------- Stub DOM ---------------- */

class ClassList {
  constructor() { this.set = new Set(); }
  add(...cs) { cs.forEach((c) => this.set.add(c)); }
  remove(...cs) { cs.forEach((c) => this.set.delete(c)); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
  contains(c) { return this.set.has(c); }
}

const windowStub = {
  scrollX: 0,
  scrollY: 0,
  scrollTo(x, y) { this.scrollX = x; this.scrollY = y; },
  addEventListener() {},
  removeEventListener() {},
  confirm() { return true; },
  VibeTheme: undefined,
  // no ResizeObserver on purpose ('ResizeObserver' in window === false)
};

/* The modeled symptom: tearing a container's children down (what renderBoard
 * does to every column on each poll) drops the window's scroll anchor, so the
 * browser scrolls the window back to the top. */
let teardowns = 0;

class StubElement {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.classList = new ClassList();
    this.dataset = {};
    this.style = { setProperty() {} };
    this.attributes = {};
    this.listeners = {};
    this.id = '';
    this._text = '';
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.draggable = false;
    this.title = '';
    this.type = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.offsetHeight = 0;
    this.scrollWidth = 0;
  }
  set className(v) { this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classList.set].join(' '); }
  set textContent(v) {
    if (v === '' && this.children.length > 0) {
      // DOM teardown during a poll: the scroll anchor node is destroyed.
      windowStub.scrollY = 0;
      teardowns++;
    }
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    this._text = String(v);
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text;
  }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  get options() { return this.children.filter((c) => c.tagName === 'OPTION'); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    c.parentNode = this;
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  dispatchEvent() { return true; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  hasAttribute(k) { return k in this.attributes; }
  matches(sel) {
    if (sel[0] === '.') return this.classList.contains(sel.slice(1).split('[')[0]);
    if (sel[0] === '#') return this.id === sel.slice(1);
    return this.tagName === sel.toUpperCase();
  }
  _walk(out, sel) {
    for (const c of this.children) {
      if (c.matches(sel)) out.push(c);
      c._walk(out, sel);
    }
    return out;
  }
  querySelector(sel) { return this._walk([], sel)[0] || null; }
  querySelectorAll(sel) { return this._walk([], sel); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 20, width: 100, height: 20 }; }
  focus() {}
  blur() {}
  closest() { return null; }
  contains() { return false; }
}

const byId = new Map();
const documentStub = {
  documentElement: new StubElement('html'),
  body: new StubElement('body'),
  getElementById(id) {
    if (!byId.has(id)) {
      const e = new StubElement('div');
      e.id = id;
      byId.set(id, e);
    }
    return byId.get(id);
  },
  createElement(tag) { return new StubElement(tag); },
  createElementNS(ns, tag) { return new StubElement(tag); },
  createTextNode(t) { const e = new StubElement('#text'); e._text = String(t); return e; },
  querySelectorAll(sel) { return this.body._walk([], sel); },
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() { return true; },
};

const localStorageStub = {
  _s: Object.create(null),
  getItem(k) { return k in this._s ? this._s[k] : null; },
  setItem(k, v) { this._s[k] = String(v); },
  removeItem(k) { delete this._s[k]; },
};

/* ---------------- Fake API ---------------- */

let liveTokens = 1000;
const note = () => ({
  id: 'abc123',
  title: 'Running note',
  body: 'do a thing',
  status: 'running',
  created_at: '2026-10-29T18:00:00+00:00',
  updated_at: '2026-10-29T19:00:00+00:00',
  project: null,
  workdir: null,
  depends_on: [],
  auto_run: false,
  auto_accept: false,
  questions: [],
  session: { started_at: '2026-10-29T19:00:00+00:00', live_tokens: liveTokens },
});

function payloadFor(url) {
  if (url.startsWith('/api/cards')) return [note()];
  if (url.startsWith('/api/stats')) {
    return {
      total_notes: 1,
      total_tokens: liveTokens,
      live_tokens: liveTokens,
      total_runtime_seconds: 5,
      avg_runtime_seconds: 5,
      by_status: { running: 1 },
    };
  }
  if (url.startsWith('/api/projects')) return [];
  if (url.startsWith('/api/templates')) return [];
  return {};
}

const fetchStub = (url) => Promise.resolve({
  ok: true,
  status: 200,
  json: () => Promise.resolve(payloadFor(url)),
});

/* ---------------- Load the real app.js ---------------- */

const src = fs.readFileSync(APP_JS, 'utf8');
const factory = new Function(
  'window', 'document', 'localStorage', 'navigator', 'fetch',
  'getComputedStyle', 'CSS', 'setInterval', 'clearInterval', 'setTimeout', 'clearTimeout',
  'requestAnimationFrame',
  src + '\n;return { refresh, renderBoard, renderStats, state };'
);

const app = factory(
  windowStub, documentStub, localStorageStub,
  { userAgentData: null, platform: 'Node' },
  fetchStub,
  () => ({ fontSize: '15px' }),
  { escape: (s) => String(s).replace(/(["\\])/g, '\\$1') },
  () => 1, () => {}, () => 0, () => {}, () => 0
);

/* ---------------- Scenario ---------------- */

async function main() {
  // Let the boot-time loadProjects()/refresh() settle.
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));

  const runningCol = byId.get('col-running');
  if (!runningCol || runningCol.children.length !== 1) {
    console.error('SETUP ERROR: board did not render the running note after boot');
    process.exit(2);
  }
  const tokensBefore = byId.get('stat-tokens').textContent;

  // The user is scrolled down the board…
  windowStub.scrollY = 800;
  windowStub.scrollX = 0;

  // …and the 2 s poll fires with an updated token count (session still live).
  liveTokens = 2500;
  await app.refresh();

  const tokensAfter = byId.get('stat-tokens').textContent;
  const y = windowStub.scrollY;

  console.log(`token count: ${tokensBefore} -> ${tokensAfter} (poll updated it: ${tokensBefore !== tokensAfter})`);
  console.log(`window.scrollY after poll: ${y} (was 800 before the poll, teardowns seen: ${teardowns})`);

  if (tokensBefore === tokensAfter) {
    console.error('SETUP ERROR: the poll did not update the token count');
    process.exit(2);
  }
  if (y === 0) {
    console.error('BUG REPRODUCED: the token-count poll scrolled the window back to the top (scrollY 800 -> 0)');
    process.exit(1);
  }
  if (y !== 800) {
    console.error(`UNEXPECTED: window.scrollY is ${y}, expected 800`);
    process.exit(2);
  }
  console.log('OK: the token-count poll kept the window scroll position (800)');
  process.exit(0);
}

main().catch((err) => {
  console.error('SETUP ERROR:', err && err.stack || err);
  process.exit(2);
});
