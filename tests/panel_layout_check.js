/* Panel layout check: the floating panels are movable, resizable and remember
 * where the user put them.
 *
 * Usage: node tests/panel_layout_check.js <path-to-app.js>
 *
 * Boots the served app.js under the shared DOM stub (tests/dom_stub.js) and
 * checks the spec:
 *
 *   - positionFloatingPanel still places a panel next to the clicked card and
 *     clamps it to the viewport;
 *   - a saved layout wins over the anchor, still clamped to the viewport;
 *   - the layout round-trips through localStorage under
 *     `vibe-harness:panel:<key>` (save -> load -> position);
 *   - a corrupt, missing or unreadable stored value falls back to the defaults
 *     and never throws (private mode throws on localStorage);
 *   - makePanelDraggable reports the end of a drag through onEnd;
 *   - a native resize — which writes the inline width/height AND the used box —
 *     is saved once, debounced, not on every frame;
 *   - opening a panel is not a resize: the harness positioning it is not the
 *     user's choice, even though the rendered box matches the inline style;
 *   - close + reopen and a reload restore the size and the place;
 *   - clearing localStorage returns both panels to their defaults;
 *   - both headers are drag handles.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const { makeDom } = require('./dom_stub.js');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: panel_layout_check.js <app.js>'); process.exit(2); }

const failures = [];
function check(ok, msg) { if (!ok) failures.push(msg); }

const EXPORTS = [
  'state', 'positionFloatingPanel', 'panelLayout', 'loadPanelLayout',
  'savePanelLayout', 'makePanelDraggable', 'rememberPanelLayout',
  'openActivity', 'closeActivity', 'openDiff', 'closeDiff',
  'ACTIVITY_WIDTH', 'ACTIVITY_HEIGHT', 'DIFF_WIDTH', 'DIFF_HEIGHT',
  'PANEL_LAYOUT_PREFIX',
];

const ACTIVITY_KEY = 'vibe-harness:panel:activity';
const DIFF_KEY = 'vibe-harness:panel:diff';

function res(payload, status) {
  const code = status || 200;
  return {
    ok: code < 400, status: code, statusText: code < 400 ? 'OK' : 'Error',
    headers: { get: () => null },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

async function fetchStub(path) {
  const p = String(path);
  if (p === '/api/cards') return res([]);
  if (p.startsWith('/api/stacks')) return res([]);
  if (p.startsWith('/api/archives')) return res([]);
  if (p.startsWith('/api/projects')) return res([]);
  if (p.startsWith('/api/models')) return res({ roles: {}, models: [], default_model: null });
  if (p.startsWith('/api/settings')) return res({ backend: 'vibe', backends: [] });
  if (p.startsWith('/api/version')) return res({ version: 'test' });
  if (p.startsWith('/api/stats')) return res({ total_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0 });
  if (p.indexOf('/activity') !== -1) return res({ cursor: 0, events: [] });
  if (p.indexOf('/messages') !== -1) return res({ messages: [] });
  if (p.indexOf('/diff') !== -1) return res({ range: 'card', diff: '', files: [], review: null });
  return res({});
}

/* The clicked card: 200x140 at (100, 200), so the panel opens to its right. */
function anchorRect() {
  return { left: 100, top: 200, width: 200, height: 140, right: 300, bottom: 340 };
}

function box(panel) {
  return {
    left: parseFloat(panel.style.left),
    top: parseFloat(panel.style.top),
    width: parseFloat(panel.style.width),
    height: parseFloat(panel.style.height),
  };
}

function sameBox(got, want, what) {
  check(got.left === want.left && got.top === want.top
    && got.width === want.width && got.height === want.height,
    what + ', got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
}

async function main() {
  const dom = makeDom({ fetch: fetchStub });
  const X = dom.boot(APP_JS, EXPORTS);
  await dom.flush();

  const activityPopup = dom.getEl('activity-popup');
  const activityHead = dom.getEl('activity-head');
  const diffPopup = dom.getEl('diff-popup');
  const diffHead = dom.getEl('diff-popup-head');
  const anchor = dom.getEl('card-anchor');
  anchor.getBoundingClientRect = anchorRect;

  /* 1. The defaults are the sizes the panels ship with. */
  check(X.ACTIVITY_WIDTH === 420 && X.ACTIVITY_HEIGHT === 520,
    'the activity default is 420x520, got ' + X.ACTIVITY_WIDTH + 'x' + X.ACTIVITY_HEIGHT);
  check(X.DIFF_WIDTH === 720 && X.DIFF_HEIGHT === 640,
    'the diff default is 720x640, got ' + X.DIFF_WIDTH + 'x' + X.DIFF_HEIGHT);
  check(X.PANEL_LAYOUT_PREFIX === 'vibe-harness:panel:',
    'the storage prefix is vibe-harness:panel:, got ' + X.PANEL_LAYOUT_PREFIX);

  /* 2. No saved layout: the panel opens next to the card, clamped. */
  X.positionFloatingPanel(activityPopup, anchor, X.ACTIVITY_WIDTH, X.ACTIVITY_HEIGHT);
  sameBox(box(activityPopup), { left: 312, top: 200, width: 420, height: 520 },
    'without a saved layout the panel opens to the right of the card');

  /* 3. A saved layout wins over the anchor. */
  X.positionFloatingPanel(activityPopup, anchor, X.ACTIVITY_WIDTH, X.ACTIVITY_HEIGHT,
    { width: 600, height: 400, left: 40, top: 60 });
  sameBox(box(activityPopup), { left: 40, top: 60, width: 600, height: 400 },
    'a saved layout is used instead of the anchor placement');

  /* 4. ...and is still clamped to the viewport (1280x800 in the stub). */
  X.positionFloatingPanel(activityPopup, anchor, X.ACTIVITY_WIDTH, X.ACTIVITY_HEIGHT,
    { width: 5000, height: 5000, left: 5000, top: 5000 });
  sameBox(box(activityPopup), { left: 12, top: 12, width: 1256, height: 776 },
    'a saved layout is clamped to the viewport');

  /* 5. Round-trip: save -> load -> position. */
  X.positionFloatingPanel(activityPopup, anchor, X.ACTIVITY_WIDTH, X.ACTIVITY_HEIGHT,
    { width: 500, height: 360, left: 80, top: 90 });
  X.savePanelLayout('activity', X.panelLayout(activityPopup));
  check(!!dom.store[ACTIVITY_KEY],
    'the layout is stored under ' + ACTIVITY_KEY + ', got ' + JSON.stringify(Object.keys(dom.store)));
  const stored = JSON.parse(dom.store[ACTIVITY_KEY] || '{}');
  check(stored.width === 500 && stored.height === 360
    && stored.left === 80 && stored.top === 90,
    'the stored layout is {width, height, left, top}, got ' + JSON.stringify(stored));
  const loaded = X.loadPanelLayout('activity');
  check(!!loaded && loaded.width === 500 && loaded.height === 360
    && loaded.left === 80 && loaded.top === 90,
    'the stored layout loads back, got ' + JSON.stringify(loaded));
  X.positionFloatingPanel(activityPopup, anchor, X.ACTIVITY_WIDTH, X.ACTIVITY_HEIGHT, loaded);
  sameBox(box(activityPopup), { left: 80, top: 90, width: 500, height: 360 },
    'the loaded layout positions the panel');

  /* 6. A corrupt or missing value falls back to the defaults, never throws. */
  dom.store[ACTIVITY_KEY] = '{not json';
  check(X.loadPanelLayout('activity') === null, 'corrupt JSON falls back to the defaults');
  dom.store[ACTIVITY_KEY] = '{"width":"wide","height":360,"left":80,"top":90}';
  check(X.loadPanelLayout('activity') === null, 'a non-numeric field falls back to the defaults');
  dom.store[ACTIVITY_KEY] = '{"width":500,"height":360}';
  check(X.loadPanelLayout('activity') === null, 'a missing field falls back to the defaults');
  dom.store[ACTIVITY_KEY] = 'null';
  check(X.loadPanelLayout('activity') === null, 'a null value falls back to the defaults');
  delete dom.store[ACTIVITY_KEY];
  check(X.loadPanelLayout('activity') === null, 'a missing value falls back to the defaults');

  /* 7. Private mode: localStorage throws on every call. */
  const realGet = dom.sandbox.localStorage.getItem;
  const realSet = dom.sandbox.localStorage.setItem;
  dom.sandbox.localStorage.getItem = () => { throw new Error('private mode'); };
  dom.sandbox.localStorage.setItem = () => { throw new Error('private mode'); };
  let threw = null;
  try {
    X.loadPanelLayout('activity');
    X.savePanelLayout('activity', { width: 500, height: 360, left: 80, top: 90 });
  } catch (err) { threw = err; }
  check(threw === null, 'a throwing localStorage never escapes the layout helpers, got ' + threw);
  dom.sandbox.localStorage.getItem = realGet;
  dom.sandbox.localStorage.setItem = realSet;

  /* 8. makePanelDraggable reports the end of a drag through onEnd. */
  let ends = 0;
  const handle = dom.getEl('drag-handle');
  const panel = dom.getEl('drag-panel');
  panel.style.left = '10px';
  panel.style.top = '20px';
  X.makePanelDraggable(panel, handle, () => { ends += 1; });
  handle.listeners.pointerdown[0]({ button: 0, clientX: 100, clientY: 100, target: handle, preventDefault: () => {} });
  handle.listeners.pointermove[0]({ clientX: 150, clientY: 130 });
  check(ends === 0, 'onEnd does not fire mid-drag, got ' + ends);
  handle.listeners.pointerup[0]({});
  check(ends === 1, 'onEnd fires when the drag ends, got ' + ends);
  handle.listeners.pointerup[0]({});
  check(ends === 1, 'a pointerup without a drag does not fire onEnd, got ' + ends);
  handle.listeners.pointerdown[0]({ button: 0, clientX: 0, clientY: 0, target: handle, preventDefault: () => {} });
  handle.listeners.pointercancel[0]({});
  check(ends === 2, 'a cancelled drag ends too, got ' + ends);

  /* 9. Both headers are drag handles. */
  check(!!(activityHead.listeners.pointerdown && activityHead.listeners.pointermove),
    'the activity header is the drag handle');
  check(!!(diffHead.listeners.pointerdown && diffHead.listeners.pointermove),
    'the diff header is still the drag handle');

  /* 10. A native resize is saved once, debounced — not on every frame. The
   *     browser's resize grip writes the inline width/height AND the used box,
   *     so after a real resize the rendered size equals the inline style. That
   *     is exactly what the old guard mistook for "the harness positioned it",
   *     which is why the size was never saved. `grip` plays the browser: both
   *     the inline style and the box move, and the ResizeObserver fires. */
  const observer = dom.resizeObservers.filter((o) => o.targets.indexOf(activityPopup) !== -1)[0];
  check(!!observer, 'the activity popup is watched for resizes');
  const grip = (panel, width, height) => {
    const left = parseFloat(panel.style.left) || 0;
    const top = parseFloat(panel.style.top) || 0;
    panel.style.width = width + 'px';
    panel.style.height = height + 'px';
    panel._rect = { left, top, width, height, right: left + width, bottom: top + height };
    dom.fireResize(panel);
  };
  delete dom.store[ACTIVITY_KEY];
  grip(activityPopup, 640, 480);
  check(!dom.store[ACTIVITY_KEY], 'a resize is not written on every frame');
  grip(activityPopup, 660, 500);
  dom.runTimers();
  const resized = JSON.parse(dom.store[ACTIVITY_KEY] || '{}');
  check(resized.width === 660 && resized.height === 500,
    'the debounced resize is saved, got ' + JSON.stringify(resized));
  delete activityPopup._rect;

  /* 10a. Opening the panel is not a resize: the anchor placement is not the
   *      user's choice, so it is not remembered. The rendered box matches the
   *      inline style here too — only the positioning mark tells them apart. */
  delete dom.store[ACTIVITY_KEY];
  await X.openActivity('card-1', anchor);
  dom.fireResize(activityPopup);   // what the browser does when the panel shows
  dom.runTimers();
  check(!dom.store[ACTIVITY_KEY],
    'opening the panel does not save a layout the user never chose');
  X.closeActivity();

  /* 10b. The layout is captured when the resize happens, not when the debounce
   *      fires: a panel closed in between must not save its hidden 0x0 box. */
  delete dom.store[ACTIVITY_KEY];
  grip(activityPopup, 700, 540);
  activityPopup._rect = { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  activityPopup.style.width = '';
  activityPopup.style.height = '';
  dom.runTimers();
  const captured = JSON.parse(dom.store[ACTIVITY_KEY] || '{}');
  check(captured.width === 700 && captured.height === 540,
    'the resize is captured at resize time, not at debounce time, got '
      + JSON.stringify(captured));
  delete activityPopup._rect;

  /* 11. Close and reopen restores the size and the place. */
  dom.store[ACTIVITY_KEY] = JSON.stringify({ width: 500, height: 360, left: 80, top: 90 });
  await X.openActivity('card-1', anchor);
  sameBox(box(activityPopup), { left: 80, top: 90, width: 500, height: 360 },
    'opening the activity popup restores the saved layout');
  X.closeActivity();
  await X.openActivity('card-1', anchor);
  sameBox(box(activityPopup), { left: 80, top: 90, width: 500, height: 360 },
    'closing and reopening keeps the layout');
  X.closeActivity();

  /* 12. A reload restores it too: a second instance over the same storage. */
  const dom2 = makeDom({ fetch: fetchStub, store: dom.store });
  const X2 = dom2.boot(APP_JS, EXPORTS);
  await dom2.flush();
  const anchor2 = dom2.getEl('card-anchor');
  anchor2.getBoundingClientRect = anchorRect;
  await X2.openActivity('card-1', anchor2);
  sameBox(box(dom2.getEl('activity-popup')), { left: 80, top: 90, width: 500, height: 360 },
    'a reload restores the saved layout');
  X2.closeActivity();

  /* 13. Dragging the activity header moves it and saves where it landed. */
  delete dom.store[ACTIVITY_KEY];
  await X.openActivity('card-1', anchor);
  activityHead.listeners.pointerdown[0]({ button: 0, clientX: 100, clientY: 100, target: activityHead, preventDefault: () => {} });
  activityHead.listeners.pointermove[0]({ clientX: 140, clientY: 120 });
  check(parseFloat(activityPopup.style.left) === 352 && parseFloat(activityPopup.style.top) === 220,
    'dragging the activity header moves the panel, got ' + activityPopup.style.left + ',' + activityPopup.style.top);
  activityHead.listeners.pointerup[0]({});
  const dragged = JSON.parse(dom.store[ACTIVITY_KEY] || '{}');
  check(dragged.left === 352 && dragged.top === 220,
    'the drag end saves where the panel landed, got ' + JSON.stringify(dragged));
  X.closeActivity();

  /* 14. Clearing localStorage returns both panels to their defaults. */
  for (const key of Object.keys(dom.store)) delete dom.store[key];
  await X.openActivity('card-1', anchor);
  sameBox(box(activityPopup), { left: 312, top: 200, width: 420, height: 520 },
    'clearing localStorage returns the activity popup to its defaults');
  X.closeActivity();
  await X.openDiff('card-1', null, anchor);
  // 640 tall in an 800 viewport: the anchor's top is clamped to 800-640-12.
  sameBox(box(diffPopup), { left: 312, top: 148, width: 720, height: 640 },
    'clearing localStorage returns the diff popup to its defaults');
  X.closeDiff();

  /* 15. The diff popup remembers its own layout, separately. */
  dom.store[DIFF_KEY] = JSON.stringify({ width: 900, height: 500, left: 30, top: 40 });
  await X.openDiff('card-1', null, anchor);
  sameBox(box(diffPopup), { left: 30, top: 40, width: 900, height: 500 },
    'the diff popup restores its own saved layout');
  X.closeDiff();
  await X.openActivity('card-1', anchor);
  sameBox(box(activityPopup), { left: 312, top: 200, width: 420, height: 520 },
    'the activity popup does not read the diff layout');
  X.closeActivity();

  /* 16. The diff popup remembers a native resize too, and reopening restores
   *     the resized box — not just the place. */
  delete dom.store[DIFF_KEY];
  await X.openDiff('card-1', null, anchor);
  dom.fireResize(diffPopup);   // the notification the positioning produces
  grip(diffPopup, 800, 560);
  dom.runTimers();
  const diffResized = JSON.parse(dom.store[DIFF_KEY] || '{}');
  check(diffResized.width === 800 && diffResized.height === 560,
    'a native resize of the diff popup is saved, got ' + JSON.stringify(diffResized));
  delete diffPopup._rect;
  X.closeDiff();
  await X.openDiff('card-1', null, anchor);
  sameBox(box(diffPopup), { left: 312, top: 148, width: 800, height: 560 },
    'reopening the diff popup restores the resized box');
  X.closeDiff();

  /* 16a. Opening the diff popup is not a resize either. */
  delete dom.store[DIFF_KEY];
  await X.openDiff('card-1', null, anchor);
  dom.fireResize(diffPopup);
  dom.runTimers();
  check(!dom.store[DIFF_KEY],
    'opening the diff popup does not save a layout the user never chose');
  X.closeDiff();

  const result = { ok: failures.length === 0, failures };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(2);
});
