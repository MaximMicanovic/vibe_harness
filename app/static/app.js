'use strict';

/* ---------------- Board layout ---------------- */

// "stopped" cards render under the Failed column, with a "stopped" tag on the card.
const COLUMNS = [
  { key: 'draft',    label: 'Draft' },
  { key: 'running',  label: 'Running' },
  { key: 'reviewing', label: 'Reviewing' },
  { key: 'done',     label: 'Done' },
  { key: 'accepted', label: 'Accepted' },
  { key: 'failed',   label: 'Failed', also: ['stopped'] },
];

/* Every keyboard shortcut, in ONE place: the Keys panel renders this list and
 * the global keydown handler below reads the same keys, so the panel cannot
 * drift from the handlers. */
const KEYBINDINGS = [
  { key: 'N', label: 'New card', hint: 'opens the card editor' },
  { key: 'Esc', label: 'Close panel', hint: 'clears a live search first, else closes the open popup or modal' },
  { key: 'D + click', label: 'Diff', hint: 'hold D and click a card to see what it changed' },
  { key: 'C + click', label: 'Live activity', hint: 'hold C and click a card to watch the model work' },
  { key: '/', label: 'Search', hint: 'focuses the search box — it searches the board and the archives' },
];

const state = {
  cards: [],
  stats: null,
  pollTimer: null,      // cards+stats poll: 2s while a card is live, 10s when idle
  pollIntervalMs: 0,    // the interval state.pollTimer is currently running at
  lastPollAt: null,     // Date.now() of the last SUCCESSFUL refresh (drives the heartbeat)
  pollError: null,      // message from the last failed refresh, null when healthy
  buildVersion: null,   // server fingerprint of the front-end assets (see checkBuild)
  logTimer: null,       // 2s log refresh, active while the viewed card is running
  logCardId: null,
  editorCardId: null,   // null => editor is creating a new card
  editorUpdatedAt: null, // updated_at the editor loaded — sent back as expected_updated_at
  templates: [],
  templatesLoaded: false,
  projects: [],
  selectedProject: '',   // '' = All projects
  failureFilter: '',     // '' = every card, else only cards that failed this way
  expandedTitles: new Set(),  // card ids whose card title shows the full name
  stacks: [],            // [{ id, col, ids: [cardId bottom→top] }] — visual card stacks per column
  dragCardId: null,      // card id currently being dragged (HTML5 drag & drop: drafts link, others stack)
  renderPending: false,  // a board rebuild deferred while a drag is in progress (see renderBoard)
  openStackId: null,     // stack id currently fanned out by hover
  archives: [],          // archives from GET /api/archives
  archivePick: null,     // null | { kind: 'card'|'cards'|'project', id, ids, name, label } — pick an archive to move into
  archiveExpanded: new Set(),  // archive ids whose card list is shown
  sidebarPanel: null,    // null | 'settings' | 'models' | 'archive' | 'split' — the open sidebar panel
  sidebarArchiveOpen: new Set(),  // archive ids expanded in the sidebar's archive browser
  sidebarProjectOpen: new Set(),  // project names expanded in the archive browser
  sidebarArchiveProjects: new Set(),  // '<archiveId>::<project>' keys the user folded away
  archiveProjects: new Map(),  // archive id -> its project groups (GET /api/archives/<id>), cached for the sidebar
  models: null,          // GET /api/models: { roles, models, default_model }
  settings: null,        // GET /api/settings: { backend, backends, failure_kinds, … }
  diffCardId: null,      // card whose diff popup is open
  diffRange: 'card',     // 'card' | 'round' | 'worktree'
  diffMode: null,        // null = auto (split when the panel is wide enough)
  diffPalette: 'default',// 'default' | 'colorblind', persisted in localStorage
  diffLast: null,        // last diff payload, so a layout toggle re-renders without a fetch
  prevStatuses: null,    // Map cardId -> status from the previous refresh (null = first load)
  notificationsEnabled: true,  // sidebar Settings toggle, persisted in localStorage
  activityCardId: null,  // card whose live activity popup is open (one at a time)
  activityCursor: 0,     // journal lines already consumed by the popup
  activityMessageSeq: 0, // highest mailbox seq already shown in the popup
  activityTimer: null,   // 1s activity poll, active while the popup's card is live
  activityAutoScroll: true,  // false once the user scrolls the feed up
  cKeyDown: false,       // C is held: a card click opens the activity popup
  dKeyDown: false,       // D is held: a card click opens the diff popup
  unreadNotifications: 0,      // finishes announced while the tab was hidden (title badge)
  searchQuery: '',       // the live query in the top bar ('' = no search)
  searchResults: [],     // GET /api/search hits: board cards + archived cards
  searchTotal: 0,        // uncapped match count the server reported
  searchTruncated: false,// the server capped the list (see X-Search-Truncated)
  searchPending: false,  // a server search is in flight (the list shows "Searching…")
  searchTimer: null,     // debounce timer for the server-side archive search
};

/* ---------------- DOM refs ---------------- */

const $ = (id) => document.getElementById(id);
const emptyBoard = $('empty-board');
// The board's "nothing here" message, restored whenever a search is cleared.
const EMPTY_BOARD_TEXT = emptyBoard.textContent;
const errorBanner = $('error-banner');
const errorText = $('error-text');

const editorOverlay = $('editor-overlay');
const editorHeading = $('editor-heading');
const templateField = $('template-field');
const templateSelect = $('template-select');
const cardTitle = $('card-title');
const cardBody = $('card-body');
const cardAutoRun = $('card-auto-run');
const cardAutoAccept = $('card-auto-accept');
const cardProject = $('card-project');
const cardWorkdir = $('card-workdir');
const dependsField = $('depends-field');
const dependsList = $('depends-list');
const projectSelect = $('project-select');
const archivesBtn = $('archives-btn');
const archivesOverlay = $('archives-overlay');
const archiveForm = $('archive-form');
const archiveNameInput = $('archive-name');
const archiveTypeInput = $('archive-type');
const archiveProjectInput = $('archive-project');
const archivePickBanner = $('archive-pick-banner');
const archiveListItems = $('archive-list-items');
const projectsOverlay = $('projects-overlay');
const projectForm = $('project-form');
const projectNameInput = $('project-name');
const projectPathInput = $('project-path');
const projectBrowseBtn = $('project-browse');
const projectListItems = $('project-list-items');
const btnSave = $('btn-save');
const btnRun = $('btn-run');
const btnStop = $('btn-stop');
const btnAccept = $('btn-accept');
const btnRevert = $('btn-revert');
const btnArchive = $('btn-archive');
const btnLog = $('btn-log');
const btnDiff = $('btn-diff');
const btnDelete = $('btn-delete');
const acceptHint = $('accept-hint');
const revertNote = $('revert-note');
const editConflict = $('edit-conflict');
const editConflictText = $('edit-conflict-text');
const btnReloadTheirs = $('btn-reload-theirs');
const btnOverwrite = $('btn-overwrite');
const cardPrLink = $('card-pr-link');

/* The auto-accept checkbox remembers the last setting used (per browser). */
const AUTO_ACCEPT_KEY = 'vibe-harness:auto-accept';
function loadAutoAcceptDefault() {
  try { return localStorage.getItem(AUTO_ACCEPT_KEY) === '1'; } catch (_) { return false; }
}
function saveAutoAcceptDefault(on) {
  try { localStorage.setItem(AUTO_ACCEPT_KEY, on ? '1' : '0'); } catch (_) { /* private mode */ }
}

/* The last used project, remembered per browser: updated whenever a card is
 * saved with a project (or without one) and whenever a concrete project is
 * picked in the top-bar switcher. The new-card dropdown defaults to it.
 * Returns null when nothing was used yet, '' when the last use had no project. */
const LAST_PROJECT_KEY = 'vibe-harness:last-project';
function loadLastProjectDefault() {
  try { return localStorage.getItem(LAST_PROJECT_KEY); } catch (_) { return null; }
}
function saveLastProjectDefault(name) {
  try { localStorage.setItem(LAST_PROJECT_KEY, name || ''); } catch (_) { /* private mode */ }
}

/* The project a new card defaults to: the last used project when it is still
 * registered, else the project selected in the top-bar switcher, else none. */
function defaultProjectForNewCard() {
  const candidates = [loadLastProjectDefault(), state.selectedProject || ''];
  for (const name of candidates) {
    if (name === null) continue;                       // nothing remembered yet
    if (name === '' || state.projects.some((p) => p.name === name)) return name;
  }
  return '';
}

/* The top-bar project switcher also remembers its selection per browser, so a
 * reload lands back on the project you were working in. '' = All projects. */
const SELECTED_PROJECT_KEY = 'vibe-harness:selected-project';
function loadSelectedProject() {
  try { return localStorage.getItem(SELECTED_PROJECT_KEY) || ''; } catch (_) { return ''; }
}
function saveSelectedProject(name) {
  try { localStorage.setItem(SELECTED_PROJECT_KEY, name || ''); } catch (_) { /* private mode */ }
}

/* Card stacks (drag a card onto another card to stack them): [{ id, col,
 * title, ids }] with ids ordered bottom→top. A stack always lives in one
 * column and holds cards from that column only.
 *
 * They are server state (stacks.json), not browser state: the MCP tools let an
 * LLM stack cards and move them between piles, and an LLM cannot reach this
 * browser's localStorage. state.stacks is the board's copy for rendering. */
const STACKS_KEY = 'vibe-harness:stacks';  // legacy: pre-server piles, migrated once

async function loadStacks() {
  const stacks = await api('/api/stacks');
  state.stacks = Array.isArray(stacks) ? stacks : [];
  return state.stacks;
}

/* One-time migration: piles used to live in localStorage. Push them to the
 * server the first time it has none, so an existing board keeps its piles. */
async function migrateLocalStacks() {
  let legacy = [];
  try { legacy = JSON.parse(localStorage.getItem(STACKS_KEY) || '[]'); } catch (_) { return; }
  if (!Array.isArray(legacy) || !legacy.length) return;
  if (state.stacks.length) { localStorage.removeItem(STACKS_KEY); return; }
  for (const s of legacy) {
    if (!s || !Array.isArray(s.ids) || s.ids.length < 2) continue;
    try {
      await api('/api/stacks', { method: 'POST', body: JSON.stringify({
        card_ids: s.ids, title: s.title || '', col: s.col,
      }) });
    } catch (_) { /* a pile whose cards changed column is simply dropped */ }
  }
  localStorage.removeItem(STACKS_KEY);
  await loadStacks();
}

const logOverlay = $('log-overlay');
const logHeading = $('log-heading');
const logContent = $('log-content');

const activityPopup = $('activity-popup');
const activityHead = $('activity-head');
const activityTitle = $('activity-title');
const activityStatus = $('activity-status');
const activityTokens = $('activity-tokens');
const activityFeed = $('activity-feed');
const activityJump = $('activity-jump');
const activityCompose = $('activity-compose');
const activityInput = $('activity-input');
const activitySend = $('activity-send');
const activityHint = $('activity-hint');

const diffPopup = $('diff-popup');
const diffPopupHead = $('diff-popup-head');
const diffHeading = $('diff-heading');
const diffRanges = $('diff-ranges');
const diffViews = $('diff-views');
const diffPaletteBtn = $('diff-palette');
const diffVerdict = $('diff-verdict');
const diffFiles = $('diff-files');
const diffBody = $('diff-body');

/* ---------------- Small helpers ---------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function parseTs(iso) {
  const t = Date.parse(iso);
  return isNaN(t) ? null : t;
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
  if (m > 0) return m + 'm ' + s + 's';
  return s + 's';
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

/* Hover suffix naming what types of tokens a counter was made of — cached,
 * output, input (in that order). Unknown types (null) are skipped; when no
 * type is known at all the suffix is empty and the tooltip stays generic. */
function tokenTypesSuffix(breakdown) {
  if (!breakdown) return '';
  const parts = [];
  if (breakdown.cached != null) parts.push('cached ' + fmtTokens(breakdown.cached));
  if (breakdown.output != null) parts.push('output ' + fmtTokens(breakdown.output));
  if (breakdown.input != null) parts.push('input ' + fmtTokens(breakdown.input));
  return parts.length ? ' — ' + parts.join(' · ') : '';
}

/* Inline text naming the token types with their counts — cached, input,
 * output (in that order), all in one string so they fit in a single little
 * bubble. Unknown types (null) are skipped; null when no type is known. */
function tokenTypesInline(breakdown) {
  if (!breakdown) return null;
  const parts = [];
  if (breakdown.cached != null) parts.push('cached ' + fmtTokens(breakdown.cached));
  if (breakdown.input != null) parts.push('input ' + fmtTokens(breakdown.input));
  if (breakdown.output != null) parts.push('output ' + fmtTokens(breakdown.output));
  return parts.length ? parts.join(' · ') : null;
}

/* Top-bar total tokens: next to the total itself, one bubble per token type
 * (cached, input, output) — there is more space here than on a card, so the
 * types each get their own bubble instead of sharing one. Types that are
 * unknown (null) are skipped; no breakdown at all -> no bubbles. */
function renderTokenTypeBubbles(container, breakdown) {
  if (!container) return;
  container.textContent = '';
  if (!breakdown) return;
  for (const label of ['cached', 'input', 'output']) {
    const v = breakdown[label];
    if (v == null) continue;
    const b = el('span', 'stat stat-tok tok-' + label, label + ' ' + fmtTokens(v));
    b.title = label + ' tokens across all cards: ' + fmtTokens(v);
    container.appendChild(b);
  }
}

function fmtUpdated(iso) {
  const t = parseTs(iso);
  if (t === null) return '';
  const d = (Date.now() - t) / 1000;
  if (d < 45) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + 'm ago';
  if (d < 86400) return Math.floor(d / 3600) + 'h ago';
  return new Date(t).toLocaleDateString();
}

/* ---------------- API wrapper + error banner ---------------- */

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail !== undefined) {
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch (_) { /* body was not JSON */ }
    const err = new Error(detail);
    err.status = res.status;   // callers branch on 409 (a lost edit race)
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

function showError(err) {
  errorText.textContent = 'Error: ' + (err && err.message ? err.message : String(err));
  errorBanner.hidden = false;
}

function hideError() {
  errorBanner.hidden = true;
  errorText.textContent = '';
}

/* Run an action, then refresh the board once it settles. */
async function action(fn) {
  hideError();
  try {
    await fn();
  } catch (err) {
    showError(err);
  }
  await refresh();
}

/* ---------------- Stats bar ---------------- */

/* Only Vibe streams live token counts (it writes a session journal the harness
 * tails). Claude reports usage once, in its final result object, so a live
 * counter would sit at zero for the whole run — hide it rather than show a
 * number that never moves. */
function backendHasLiveTokens() {
  return !state.settings || state.settings.backend !== 'claude';
}

function renderStats() {
  const s = state.stats;
  if (!s) return;
  const live = backendHasLiveTokens();
  $('stat-tokens-wrap').hidden = !live;
  if (!live) {
    $('stat-runtime').textContent = fmtDuration(s.total_runtime_seconds);
    $('stat-avg').textContent = fmtDuration(s.avg_runtime_seconds);
    return;
  }
  // Only the stats that are unique to the top bar — per-status counts are
  // already shown in each board column's header.
  $('stat-tokens').textContent = fmtTokens(s.total_tokens);
  // Hover the counter to see what types of tokens it was made of.
  $('stat-tokens-wrap').title = 'Total tokens across all cards: '
    + fmtTokens(s.total_tokens) + tokenTypesSuffix(s.token_breakdown);
  // The total has room for three separate bubbles — one per token type.
  renderTokenTypeBubbles($('stat-tok-types'), s.token_breakdown);
  // While any session is live, its tokens update on every poll — flag that.
  $('stat-tokens-wrap').classList.toggle('stat-live', (s.live_tokens || 0) > 0);
  $('stat-runtime').textContent = fmtDuration(s.total_runtime_seconds);
  $('stat-avg').textContent = fmtDuration(s.avg_runtime_seconds);
}

/* ---------------- Board / cards ---------------- */

function chip(className, text) {
  return el('span', 'chip chip-' + className, text);
}

/* The model picker that sits on every card. Changing it PATCHes the card, so a
 * card can be pinned to a model (or put back on "inherit") without opening the
 * editor — and a model that splits a spec can do the same through the MCP. */
function buildCardModelPicker(card) {
  const wrap = el('label', 'card-model');
  wrap.title = card.model
    ? 'This card runs on ' + card.model + ' — click to change'
    : 'This card inherits the worker role\'s model — click to pin one';
  const select = el('select', 'card-model-select');
  const inherit = el('option', '', card.model ? 'model: inherit' : 'model: inherit');
  inherit.value = '';
  select.appendChild(inherit);
  const models = (state.models && state.models.models) || [];
  for (const m of models) {
    const opt = el('option', '', m.display_name || m.alias);
    opt.value = m.alias;
    select.appendChild(opt);
  }
  // A card pinned to a model that is no longer configured still shows it.
  if (card.model && !models.some((m) => m.alias === card.model)) {
    const opt = el('option', '', card.model);
    opt.value = card.model;
    select.appendChild(opt);
  }
  select.value = card.model || '';
  select.addEventListener('click', (e) => e.stopPropagation());
  select.addEventListener('mousedown', (e) => e.stopPropagation());
  select.addEventListener('change', (e) => {
    e.stopPropagation();
    setCardModel(card.id, select.value).catch(showError);
  });
  wrap.appendChild(select);
  return wrap;
}

/* Pin a card to a model ('' = inherit). The board re-reads the cards after. */
async function setCardModel(cardId, model) {
  await api('/api/cards/' + encodeURIComponent(cardId), {
    method: 'PUT', body: JSON.stringify({ model: model }),
  });
  await refresh();
}

/* A done card with an open merge conflict spins while a resolver session runs. */
function isResolving(card) {
  const s = card.session || {};
  return !!(s.git && s.git.resolving === true);
}

/* A draft whose run is queued — either for a free session slot (the
 * concurrency cap) or behind an in-progress merge conflict. The card spins
 * until the run starts. */
function isQueued(card) {
  const s = card.session || {};
  return s.queued === true;
}

/* The queue chip. Two things queue a run: no free session slot (the
 * concurrency cap) and an in-progress merge conflict in the card's repo. A
 * capacity wait is normal traffic, not an error — it gets the neutral
 * "waiting" style and the reason the server recorded ("waiting for a free
 * slot (2/3 running)"); only a merge conflict keeps the red chip. */
function buildQueueChip(session) {
  const reason = session.queue_reason || '';
  const capacity = reason.indexOf('waiting for a free slot') === 0;
  const c = chip(capacity ? 'waiting' : 'merge-conflict',
                 '⏳ ' + (reason || 'waiting for merge conflict to resolve'));
  c.title = capacity
    ? reason + ' — the run starts automatically when a session ends'
    : (reason || 'run queued until the in-progress merge conflict resolves');
  return c;
}

/* ---------------- Why a card failed ---------------- */

/* The short label the badge shows for each of the classifier's kinds
 * (app/failures.py). The server attaches `session.failure = {kind, detail}` to
 * every card that failed; the detail is the one-line reason, shown as the
 * badge's tooltip.
 *
 * This object is LABELS ONLY. Which kinds exist is the classifier's business:
 * GET /api/settings carries `failure_kinds` (failures.KINDS) and the filter is
 * built from that, so adding a kind there reaches the board without a JS edit.
 * The keys below double as the fallback for the first paint, before the
 * settings fetch resolves — keep them in the classifier's order. */
const FAILURE_LABELS = {
  stopped_by_user: 'stopped by user',
  interrupted: 'interrupted',
  git_conflict: 'merge conflict',
  timeout: 'timed out',
  tests_failed: 'tests failed',
  reviewer_rejected: 'reviewer rejected',
  no_verdict: 'no verdict',
  crashed: 'crashed',
};

const FAILURE_FILTER_KEY = 'vibe-harness:failure-filter';

function failureOf(card) {
  const s = card && card.session;
  return (s && s.failure) || null;
}

function failureLabel(kind) {
  return FAILURE_LABELS[kind] || String(kind || '').replace(/_/g, ' ');
}

/* Every kind the classifier can return: the server's list once it has landed,
 * else the fallback above (same order, so the filter does not reshuffle). */
function failureKinds() {
  const fromServer = state.settings && state.settings.failure_kinds;
  return Array.isArray(fromServer) && fromServer.length
    ? fromServer
    : Object.keys(FAILURE_LABELS);
}

function loadFailureFilter() {
  try {
    const kind = localStorage.getItem(FAILURE_FILTER_KEY) || '';
    // A kind that no longer exists (an older board) falls back to "any reason".
    return failureKinds().indexOf(kind) !== -1 ? kind : '';
  } catch (e) { return ''; }
}

function saveFailureFilter(kind) {
  try {
    if (kind) localStorage.setItem(FAILURE_FILTER_KEY, kind);
    else localStorage.removeItem(FAILURE_FILTER_KEY);
  } catch (e) { /* private mode: the filter just does not persist */ }
}

/* Fill the top-bar filter: "any reason" plus one entry per kind. */
function renderFailureFilter() {
  const select = $('failure-filter');
  if (!select) return;
  const kinds = failureKinds();
  // A stored kind the classifier no longer returns would narrow the board to
  // nothing with no option to undo it: drop it and show everything again.
  if (state.failureFilter && kinds.indexOf(state.failureFilter) === -1) {
    state.failureFilter = '';
    saveFailureFilter('');
  }
  select.textContent = '';
  const any = el('option', '', 'any reason');
  any.value = '';
  select.appendChild(any);
  for (const kind of kinds) {
    const opt = el('option', '', failureLabel(kind));
    opt.value = kind;
    select.appendChild(opt);
  }
  select.value = state.failureFilter || '';
}

/* Display name for a dependency id (the linked card's title, or the raw id
 * when that card is not on the board / was deleted). */
function depName(id) {
  const n = state.cards.find((x) => x.id === id);
  return n ? (n.title || id) : id;
}

function clearDropTargets() {
  document.querySelectorAll('.card-drop-target').forEach((c) => c.classList.remove('card-drop-target'));
}

/* A stable per-card tilt, derived from the card id. It used to come from
 * :nth-child(odd/even), so a card's angle depended on its position among its
 * siblings — re-sorting the column or inserting a stack above rotated every
 * card below it. */
function tiltFor(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return (h & 1 ? -0.6 : 0.5) + 'deg';
}

/* The dependencies this card is still waiting on. A link is satisfied only
 * when the card it points at is MERGED (accepted) — the same rule the server
 * uses to release the run, so the arrow disappears exactly when the wait ends.
 * A dependency that is not on the board (deleted, or archived) counts as
 * satisfied, matching the server's unmet_dependencies(). */
function unmetDeps(cardData, index) {
  const byId = index || new Map(state.cards.map((n) => [n.id, n]));
  return (cardData.depends_on || []).filter((id) => {
    const dep = byId.get(id);
    return dep ? dep.status !== 'accepted' : false;
  });
}

/* The "Accept all" button at the bottom of Done. It merges the cards that were
 * in Done when it was clicked, one at a time; a card that finishes afterwards
 * renders BELOW it and is picked up by the next click. */
function buildAcceptAll(batchCount, restCount) {
  const wrap = el('div', 'accept-all');
  wrap.dataset.acceptAll = '1';
  const btn = el('button', 'accept-all-btn');
  btn.type = 'button';
  if (batchCount) {
    btn.textContent = 'Merging… (' + batchCount + ' queued)';
    btn.disabled = true;
    btn.title = 'Merging the cards that were in Done when the batch started';
  } else {
    btn.textContent = 'Accept all (' + restCount + ')';
    btn.title = 'Merge every card in Done, one at a time';
    btn.addEventListener('click', () => action(async () => {
      const res = await api('/api/cards/accept-all', { method: 'POST' });
      if (!res || !res.queued || !res.queued.length) {
        showError(new Error('nothing to accept'));
      }
      await refresh();
    }));
  }
  wrap.appendChild(btn);
  return wrap;
}

/* Position in the "Accept all" merge queue. 1 is the card being merged now;
 * the rest merge in that order, so the number is the promise the board makes
 * about what happens next. */
function buildQueuePosBadge(pos) {
  const badge = el('span', 'queue-pos', String(pos));
  badge.title = pos === 1
    ? 'Merging now — first in the queue'
    : 'Queued #' + pos + ' — merges after the cards above it';
  if (pos === 1) badge.classList.add('queue-pos--current');
  return badge;
}

function buildCard(cardData, queuePos) {
  const card = el('article', 'card card--' + cardData.status);
  card.dataset.cardId = cardData.id;
  card.style.setProperty('--tilt', tiltFor(cardData.id));

  const head = el('div', 'card-head');
  if (cardData.status === 'running' || cardData.status === 'reviewing' || isResolving(cardData) || isQueued(cardData)) head.appendChild(el('span', 'spinner'));
  if (queuePos) head.appendChild(buildQueuePosBadge(queuePos));
  const titleText = cardData.title || '(untitled)';
  const title = el('h3', 'card-title', titleText);
  title.title = titleText;  // the full name is always reachable on hover
  if (state.expandedTitles.has(cardData.id)) title.classList.add('card-title--expanded');
  head.appendChild(title);
  // "more ▾ / less ▴" — lifts the two-line clamp so a long name displays in
  // full. Hidden until fitCardTitle finds the title still clamped; the
  // expanded state survives the board's periodic re-renders via the set above.
  const titleToggle = el('button', 'card-title-toggle', 'more ▾');
  titleToggle.type = 'button';
  titleToggle.hidden = true;
  titleToggle.setAttribute('aria-expanded', 'false');
  titleToggle.addEventListener('click', (e) => {
    e.stopPropagation();  // don't open the editor
    const expanded = title.classList.toggle('card-title--expanded');
    if (expanded) state.expandedTitles.add(cardData.id);
    else state.expandedTitles.delete(cardData.id);
    title.style.fontSize = '';  // expanded shows the full name at base size
    fitCardTitle(card);
    // The card just grew/shrank: if it sits in a stack, the stack box height
    // (--card-h) must follow immediately — otherwise the absolutely
    // positioned card overflows the stack and paints over the next item
    // until the next board re-render re-measures it.
    const stackEl = card.closest('.stack');
    if (stackEl) layoutStack(stackEl);
    renderStrings();  // the card's position changed
  });
  head.appendChild(titleToggle);
  if (cardData.status === 'stopped') head.appendChild(el('span', 'tag tag-stopped', 'stopped'));
  if (cardData.status === 'reviewing') head.appendChild(el('span', 'tag tag-reviewing', 'reviewing'));
  // Why it failed, in one word on the card and the full sentence on hover —
  // the classifier's verdict (session.failure), not a log to read.
  const failure = failureOf(cardData);
  if (failure) {
    const badge = el('span', 'tag tag-failure tag-failure--' + failure.kind,
                     '⚠ ' + failureLabel(failure.kind));
    badge.title = failure.detail || failureLabel(failure.kind);
    badge.dataset.failureKind = failure.kind;
    head.appendChild(badge);
  }
  if (cardData.project) head.appendChild(el('span', 'tag tag-project', cardData.project));
  // Split tree: a card that was split out of another shows its parent, and a
  // card that was split shows how many sub-cards it produced. Both jump to the
  // other card, so the tree is navigable without a separate view.
  const parentCard = cardData.parent_id ? state.cards.find((n) => n.id === cardData.parent_id) : null;
  if (parentCard) {
    const chip = el('button', 'tag tag-parent', '↳ ' + parentCard.title);
    chip.type = 'button';
    chip.title = 'Split out of “' + parentCard.title + '” — click to jump to it';
    chip.addEventListener('click', (e) => { e.stopPropagation(); jumpToCard(parentCard.id); });
    head.appendChild(chip);
  }
  const kids = state.cards.filter((n) => n.parent_id === cardData.id);
  if (kids.length) {
    const chip = el('button', 'tag tag-children', '⤷ ' + kids.length + (kids.length === 1 ? ' sub-card' : ' sub-cards'));
    chip.type = 'button';
    chip.title = 'Split into: ' + kids.map((k) => k.title).join(', ') + ' — click to jump to the first';
    chip.addEventListener('click', (e) => { e.stopPropagation(); jumpToCard(kids[0].id); });
    head.appendChild(chip);
  }
  card.appendChild(head);

  const snippet = el('p', 'card-snippet', (cardData.body || '').replace(/\s+/g, ' ').trim());
  card.appendChild(snippet);

  // Q&A with the running model: a pending question (harness_ask_user) gets an
  // answer box right on the card; resolved ones render as a compact history.
  const qs = cardData.questions || [];
  const pendingQ = qs.find((q) => q.status === 'pending');
  if (pendingQ) card.appendChild(buildQuestionBox(cardData, pendingQ));
  const resolvedQs = qs.filter((q) => q.status !== 'pending');
  if (resolvedQs.length) card.appendChild(buildQuestionHistory(resolvedQs));

  const s = cardData.session;
  const running = (cardData.status === 'running' || cardData.status === 'reviewing') && s && s.started_at;

  const meta = el('div', 'card-meta');
  meta.appendChild(el('span', 'card-time', fmtUpdated(cardData.updated_at)));
  if (running) {
    const live = el('span', 'card-elapsed');
    live.appendChild(document.createTextNode('⏱ '));
    const t = el('span');
    t.dataset.elapsedFrom = s.started_at;
    t.textContent = fmtDuration((Date.now() - parseTs(s.started_at)) / 1000);
    live.appendChild(t);
    meta.appendChild(live);
  }
  card.appendChild(meta);

  const chips = el('div', 'card-chips');
  // The card's own model, right on the card: a card pinned to a model runs its
  // own sessions on it (worker + merge resolver); "inherit" follows the
  // worker role from the sidebar's Models panel.
  chips.appendChild(buildCardModelPicker(card));
  if (cardData.workdir) {
    const wc = chip('workdir', '📁 ' + cardData.workdir);
    wc.title = cardData.workdir + ' — Vibe works in this folder';
    chips.appendChild(wc);
  }
  const waitingOn = unmetDeps(cardData);
  if (waitingOn.length) {
    const names = waitingOn.map(depName);
    const dc = chip('depends', '⛓ runs after ' + names.join(', '));
    dc.title = 'This card runs automatically once ' + names.join(', ')
      + ' is merged (accepted) — a red string links the cards. Remove the link in the editor.';
    chips.appendChild(dc);
  }
  if (s) {
    if (Array.isArray(s.waiting_for) && s.waiting_for.length) {
      const wc = chip('waiting', '⏳ waiting for ' + s.waiting_for.map(depName).join(', '));
      wc.title = s.queue_reason || 'waiting for dependencies to be merged — the run starts automatically';
      chips.appendChild(wc);
    }
    if (s.queued === true) {
      chips.appendChild(buildQueueChip(s));
    }
    if (s.started_at) {
      if (running) {
        // Live-ticking duration chip while the session runs.
        const c = chip('duration', '');
        c.dataset.elapsedFrom = s.started_at;
        c.textContent = fmtDuration((Date.now() - parseTs(s.started_at)) / 1000);
        chips.appendChild(c);
      } else if (s.finished_at) {
        chips.appendChild(chip('duration', fmtDuration((parseTs(s.finished_at) - parseTs(s.started_at)) / 1000)));
      }
    }
    if (typeof s.live_tokens === 'number' && backendHasLiveTokens()) {
      // Live-updating tokens of the in-flight session (server-attached while
      // the card is running/reviewing; refreshed by the 2s poll). The little
      // bubble shows the token types — cached, input, output — in the same
      // chip; the live total stays in the hover title.
      const inline = tokenTypesInline(s.live_token_breakdown);
      const lc = chip('tokens tokens-live', '⚡ ' + (inline || fmtTokens(s.live_tokens) + ' tok'));
      lc.title = 'Live total for this card (worker + review + resolver rounds so far): '
        + fmtTokens(s.live_tokens) + ' tok — updates while it runs'
        + tokenTypesSuffix(s.live_token_breakdown);
      chips.appendChild(lc);
    } else if (typeof s.card_tokens === 'number' || typeof s.tokens === 'number') {
      // The card's TOTAL cost — worker + review + resolver rounds — not just
      // the worker's export total. Same little bubble, carrying cached, input,
      // output in one chip when the breakdown is known.
      const total = typeof s.card_tokens === 'number' ? s.card_tokens : s.tokens;
      const bd = s.card_token_breakdown || s.token_breakdown;
      const inline = tokenTypesInline(bd);
      const tc = chip('tokens', inline || fmtTokens(total) + ' tok');
      tc.title = 'Total tokens this card cost (worker + review + resolver rounds): '
        + fmtTokens(total) + ' tok' + tokenTypesSuffix(bd);
      chips.appendChild(tc);
    }
    if (s.verified === true) chips.appendChild(chip('verified', '✓ verified'));
    if (s.review) {
      if (s.review.approved === true) chips.appendChild(chip('reviewed', '✓ reviewed'));
      else if (s.review.exhausted === true) {
        const rc = chip('review-exhausted', '⚠ review rounds exhausted — accept manually');
        rc.title = s.review.reason || 'reviewer never approved';
        chips.appendChild(rc);
      } else if (s.review.approved === false) {
        const isTest = s.review.source === 'test';
        const rc = chip(isTest ? 'test-failed' : 'review-rejected',
                        isTest ? '✗ tests failed' : '✗ review rejected');
        rc.title = s.review.reason || (isTest ? 'the test command failed' : 'rejected by the reviewer');
        chips.appendChild(rc);
      }
    }
    if (s.diff_stat && (s.diff_stat.added || s.diff_stat.removed)) {
      // The growth signal: how much this card added and removed against its
      // base branch. A card that adds 900 lines for a small spec is visible at
      // a glance here instead of only in review.
      const ds = chip('diff-stat', '+' + s.diff_stat.added + ' \u2212' + s.diff_stat.removed);
      const files = s.diff_stat.files || 0;
      ds.title = 'This card changed ' + files + (files === 1 ? ' file' : ' files')
        + ': +' + s.diff_stat.added + ' \u2212' + s.diff_stat.removed
        + ' lines against its base branch';
      chips.appendChild(ds);
    }
    if (s.git && s.git.branch) {
      if (s.git.merged === true) {
        chips.appendChild(chip('git-merged', '✓ merged into ' + (s.git.base_branch || '?')));
      } else {
        const gc = chip('git', '⎇ ' + s.git.branch + (s.git.base_branch ? ' → ' + s.git.base_branch : ''));
        if (s.git.merge_error && !s.git.merge_conflict) gc.title = s.git.merge_error;
        else if (s.git.stashed) gc.title = 'leftover changes were stashed before this run';
        chips.appendChild(gc);
        if (s.git.merge_conflict === true) {
          const mc = chip('merge-conflict',
            s.git.resolving === true ? '⚠ merge conflict — resolving…' : '⚠ merge conflict — Accept retries');
          mc.title = s.git.resolver_error || s.git.merge_error || 'the accept merge conflicted';
          chips.appendChild(mc);
        }
      }
    }
    if (s.reverted_at) {
      // The accept was undone: the card is a draft again, and the note says
      // when and which commit undid the merge.
      const rc = chip('reverted', '↩ reverted');
      rc.title = 'Reverted on ' + new Date(s.reverted_at).toLocaleString()
        + (s.revert_commit ? ' — revert commit ' + s.revert_commit.slice(0, 7) : '')
        + '. The card is a draft again; its session history is kept.';
      chips.appendChild(rc);
    }
    if (s.pr_url) {
      // An accepted card shipped as a pull request: the link is the audit
      // trail, so it sits on the card itself (clicking it must not open the
      // editor).
      const pc = el('a', 'chip chip-pr', '↗ PR' + (s.pr_number ? ' #' + s.pr_number : ''));
      pc.href = s.pr_url;
      pc.target = '_blank';
      pc.rel = 'noopener';
      pc.title = 'Pull request opened on accept: ' + s.pr_url;
      pc.addEventListener('click', (e) => e.stopPropagation());
      chips.appendChild(pc);
    }
    if (s.model) chips.appendChild(chip('model', s.model));
    if (cardData.auto_accept) chips.appendChild(chip('auto-accept', 'auto-accept on'));
  }
  if (chips.childNodes.length > 0) card.appendChild(chips);

  card.addEventListener('click', () => {
    // Hold a letter and click a card: D opens its diff, C watches the model
    // work — both small floating popups, not a mode you have to remember you
    // are in. A plain click still opens the editor.
    if (state.dKeyDown) openDiff(cardData.id, null, card).catch(showError);
    else if (state.cKeyDown) openActivity(cardData.id, card);
    else openEditor(cardData.id).catch(showError);
  });

  // Drag a DRAFT card onto another card to link them: the dragged draft runs
  // automatically once the card it is dropped on is merged (accepted).
  if (cardData.status === 'draft') {
    card.draggable = true;
    card.addEventListener('dragstart', (e) => {
      state.dragCardId = cardData.id;
      e.dataTransfer.setData('text/plain', cardData.id);
      e.dataTransfer.effectAllowed = 'move';
      card.classList.add('card-dragging');
    });
    card.addEventListener('dragend', () => {
      state.dragCardId = null;
      card.classList.remove('card-dragging');
      clearDropTargets();
      if (state.renderPending) renderBoard();  // flush the rebuild deferred mid-drag
    });
  }
  card.addEventListener('dragover', (e) => {
    if (!state.dragCardId || state.dragCardId === cardData.id) return;
    const src = state.cards.find((n) => n.id === state.dragCardId);
    if (!src || src.status !== 'draft') return;
    e.preventDefault();  // allow the drop
    e.dataTransfer.dropEffect = 'link';
    card.classList.add('card-drop-target');
  });
  card.addEventListener('dragleave', () => card.classList.remove('card-drop-target'));
  card.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation();
    card.classList.remove('card-drop-target');
    const srcId = state.dragCardId || e.dataTransfer.getData('text/plain');
    state.dragCardId = null;
    if (!srcId || srcId === cardData.id) return;
    const src = state.cards.find((n) => n.id === srcId);
    if (!src || src.status !== 'draft') return;  // only drafts link on drop — other cards stack instead
    // srcId (the dragged draft) will run after cardData.id (the drop target).
    action(async () => {
      await api('/api/cards/' + encodeURIComponent(srcId) + '/depends/' + encodeURIComponent(cardData.id),
                { method: 'POST' });
    });
  });
  return card;
}

/* ---------------- Card questions (harness_ask_user) ---------------- */

/* Answer (or cancel) a pending question from the card; then refresh the board. */
async function submitQuestionAnswer(cardId, questionId, answer) {
  hideError();
  try {
    await api(`/api/cards/${cardId}/questions/${questionId}/answer`, {
      method: 'POST', body: JSON.stringify({ answer }),
    });
  } catch (err) {
    showError(err);
  }
  await refresh();
}

async function cancelQuestion(cardId, questionId) {
  hideError();
  try {
    await api(`/api/cards/${cardId}/questions/${questionId}/cancel`, { method: 'POST' });
  } catch (err) {
    showError(err);
  }
  await refresh();
}

/* The pending question box: question text, optional option buttons, a free
 * text input + Answer button, and a dismiss (cancel) button. */
function buildQuestionBox(card, q) {
  const box = el('div', 'card-question');
  box.addEventListener('click', (e) => e.stopPropagation());  // don't open the editor

  const head = el('div', 'card-question-head');
  head.appendChild(el('span', 'card-question-badge', '❓ waiting for your answer'));
  const dismiss = el('button', 'icon-btn card-question-dismiss', '×');
  dismiss.type = 'button';
  dismiss.title = 'Dismiss — the session resumes with a "cancelled" result';
  dismiss.addEventListener('click', (e) => {
    e.stopPropagation();
    cancelQuestion(card.id, q.id).catch(showError);
  });
  head.appendChild(dismiss);
  box.appendChild(head);

  box.appendChild(el('p', 'card-question-text', q.question));

  if (Array.isArray(q.options) && q.options.length) {
    const opts = el('div', 'card-question-options');
    for (const opt of q.options) {
      const b = el('button', 'btn q-option', opt);
      b.type = 'button';
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        submitQuestionAnswer(card.id, q.id, opt).catch(showError);
      });
      opts.appendChild(b);
    }
    box.appendChild(opts);
  }

  const row = el('div', 'q-answer-row');
  const input = el('input', 'q-input');
  input.type = 'text';
  input.placeholder = 'Type your answer…';
  input.autocomplete = 'off';
  const send = el('button', 'btn btn-primary q-send', 'Answer');
  send.type = 'button';
  const doSend = () => {
    const text = input.value.trim();
    if (!text) return;
    submitQuestionAnswer(card.id, q.id, text).catch(showError);
  };
  send.addEventListener('click', (e) => { e.stopPropagation(); doSend(); });
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') doSend();
  });
  row.appendChild(input);
  row.appendChild(send);
  box.appendChild(row);
  return box;
}

/* Compact Q → A history of resolved questions on the card. */
function buildQuestionHistory(resolved) {
  const box = el('div', 'card-question-history');
  for (const q of resolved.slice(-3)) {
    const line = el('p', 'q-history-line');
    line.appendChild(el('span', 'q-history-q', '❓ ' + q.question));
    if (q.status === 'answered') {
      line.appendChild(el('span', 'q-history-a', '→ ' + (q.answer || '')));
    } else {
      line.appendChild(el('span', 'q-history-cancelled', '(cancelled)'));
    }
    box.appendChild(line);
  }
  return box;
}

/* ---------------- Dynamic card-title fit ---------------- */

// Long names: while the title is clamped by the two-line clamp, step its font
// size down from the CSS base (0.5 px steps, floor below) until the full name
// fits. A name that still doesn't fit gets the header's "more ▾" toggle.
const TITLE_FONT_FLOOR_PX = 11;
const TITLE_FONT_STEP_PX = 0.5;

function fitCardTitle(card) {
  const title = card.querySelector('.card-title');
  const toggle = card.querySelector('.card-title-toggle');
  if (!title || !toggle) return;
  if (title.classList.contains('card-title--expanded')) {
    // Expanded: the full name shows at base size; only the toggle label matters.
    if (toggle.hidden) toggle.hidden = false;
    if (toggle.textContent !== 'less ▴') toggle.textContent = 'less ▴';
    if (toggle.getAttribute('aria-expanded') !== 'true') toggle.setAttribute('aria-expanded', 'true');
    card._fitW = card.clientWidth;
    return;
  }
  // Skip when this card was already fitted at the current width: the
  // ResizeObserver re-fires on any column height change (e.g. a stack fanning
  // open), and re-running the reset+step measurement then would resize the
  // card again — an endless RO feedback loop that made the board shake.
  const w = card.clientWidth;
  if (card._fitW === w && title.style.fontSize !== '') { card._fitW = w; return; }
  title.style.fontSize = '';  // measure at the CSS base size
  let size = parseFloat(getComputedStyle(title).fontSize) || 15;
  while (size > TITLE_FONT_FLOOR_PX && title.scrollHeight > title.clientHeight + 1) {
    size = Math.max(TITLE_FONT_FLOOR_PX, size - TITLE_FONT_STEP_PX);
    title.style.fontSize = size + 'px';
  }
  // Still clamped at the floor? Offer the expand toggle; otherwise the full
  // name already fits and the toggle stays hidden. Write only on change:
  // unconditional attribute writes on every ResizeObserver fire feed a
  // mutation/layout feedback loop (board shake).
  const clamped = title.scrollHeight > title.clientHeight + 1;
  if (toggle.hidden === clamped) toggle.hidden = !clamped;
  if (toggle.textContent !== 'more ▾') toggle.textContent = 'more ▾';
  if (toggle.getAttribute('aria-expanded') !== 'false') toggle.setAttribute('aria-expanded', 'false');
  card._fitW = w;
}

/* Re-fit card titles when the columns resize (window resize, responsive
 * layout switch): a name that fit may start clamping, or vice versa. */
const cardsResizeObserver = ('ResizeObserver' in window)
  ? new ResizeObserver((entries) => {
      for (const entry of entries) {
        entry.target.querySelectorAll('.card').forEach(fitCardTitle);
        // Card heights may have changed — re-measure the stack fan layouts.
        entry.target.querySelectorAll('.stack').forEach(layoutStack);
      }
      renderStrings();  // card positions changed — re-draw the red strings
    })
  : null;
if (cardsResizeObserver) {
  for (const col of COLUMNS) cardsResizeObserver.observe($('col-' + col.key));
}
window.addEventListener('resize', renderStrings);

/* ---------------- Card stacks (drag to stack, hover to fan out) ---------------- */

/* The board column a card currently renders in ('' statuses map via `also`). */
function cardColumn(card) {
  const col = COLUMNS.find((c) => [c.key].concat(c.also || []).indexOf(card.status) !== -1);
  return col ? col.key : null;
}

function findStackOf(cardId) {
  return state.stacks.find((s) => s.ids.indexOf(cardId) !== -1) || null;
}

/* Drop stack members that no longer exist, moved to another column, or landed
 * in a second stack; dissolve stacks that fell below two cards. The server
 * prunes the same way (StackStore._prune) — this keeps the current render
 * honest between polls. */
function cleanStacks() {
  const byId = new Map(state.cards.map((n) => [n.id, n]));
  const seen = new Set();
  const next = [];
  for (const s of state.stacks) {
    const ids = [];
    for (const id of s.ids) {
      const n = byId.get(id);
      if (n && !seen.has(id) && cardColumn(n) === s.col) {
        seen.add(id);
        ids.push(id);
      }
    }
    if (ids.length >= 2) next.push({ id: s.id, col: s.col, title: s.title || '', ids: ids });
  }
  const changed = JSON.stringify(next) !== JSON.stringify(state.stacks);
  state.stacks = next;
  return changed;
}

/* Every stack mutation goes through the server (it owns the piles, so the MCP
 * tools and the board never disagree), then the board re-reads them. */
async function persistStacks(work) {
  try {
    await work();
    await loadStacks();
    renderBoard();
  } catch (err) { showError(err); }
}

/* Drop `dragId` onto `targetId`'s card (same column only). If the target is
 * already in a stack, the dragged card joins it; otherwise a new stack is
 * created around the target. Either way the dragged card slides UNDER the
 * target, so the card below (the drop target) stays on top of the stack. */
function stackCards(dragId, targetId) {
  if (!dragId || dragId === targetId) return;
  const dragCard = state.cards.find((n) => n.id === dragId);
  const targetCard = state.cards.find((n) => n.id === targetId);
  if (!dragCard || !targetCard) return;
  const col = cardColumn(targetCard);
  if (!col || cardColumn(dragCard) !== col) return;  // stacking is per-column
  const dragStack = findStackOf(dragId);
  if (dragStack && dragStack.ids.indexOf(targetId) !== -1) return;  // same stack already
  const targetStack = findStackOf(targetId);
  if (targetStack) {
    // Keep the target (the card below) on top: the dragged card goes directly
    // under it, at the end of ids bottom→top.
    const ids = targetStack.ids.filter((id) => id !== dragId && id !== targetId);
    ids.push(dragId, targetId);
    persistStacks(() => api('/api/stacks/' + targetStack.id, {
      method: 'PATCH', body: JSON.stringify({ card_ids: ids }),
    }));
  } else {
    persistStacks(() => api('/api/stacks', {
      method: 'POST', body: JSON.stringify({ card_ids: [dragId, targetId], col: col }),
    }));
  }
}

/* Drag a stacked card onto the column background to pull it out of the stack. */
function unstackCard(cardId) {
  if (!findStackOf(cardId)) return;
  persistStacks(() => api('/api/cards/' + cardId + '/unstack', { method: 'POST' }));
}

/* Dissolve a whole stack: every member becomes a plain card again. */
function unstackAll(stackId) {
  if (!state.stacks.some((s) => s.id === stackId)) return;
  if (state.openStackId === stackId) state.openStackId = null;
  persistStacks(() => api('/api/stacks/' + stackId, { method: 'DELETE' }));
}

/* Rename a stack (its header). */
function renameStack(stackId, title) {
  persistStacks(() => api('/api/stacks/' + stackId, {
    method: 'PATCH', body: JSON.stringify({ title: title }),
  }));
}

function clearDropMarks() {
  document.querySelectorAll('.card--drop, .cards--drop').forEach((n) => n.classList.remove('card--drop', 'cards--drop'));
}

/* The dragged card: remember which card is in flight so drop targets can
 * decide (same column, not the same stack) without parsing dataTransfer. */
function wireCardDrag(card, cardData) {
  if (cardData.status === 'draft') return;  // drafts are wired for dependency drags in buildCard
  card.draggable = true;
  card.addEventListener('dragstart', (e) => {
    if (!e.dataTransfer) return;
    e.dataTransfer.setData('text/plain', cardData.id);
    e.dataTransfer.effectAllowed = 'move';
    state.dragCardId = cardData.id;
    requestAnimationFrame(() => card.classList.add('card--dragging'));
  });
  card.addEventListener('dragend', () => {
    state.dragCardId = null;
    card.classList.remove('card--dragging');
    clearDropMarks();
    if (state.renderPending) renderBoard();  // flush the rebuild deferred mid-drag
  });
}

/* Another card as a drop target: dropping stacks the dragged card under it. */
function wireCardDrop(card, cardData) {
  card.addEventListener('dragover', (e) => {
    const dragId = state.dragCardId;
    if (!dragId || dragId === cardData.id) return;
    const dragCard = state.cards.find((n) => n.id === dragId);
    if (!dragCard || dragCard.status === 'draft') return;  // a draft drop links a dependency (buildCard), never stacks
    if (cardColumn(dragCard) !== cardColumn(cardData)) return;
    const dragStack = findStackOf(dragId);
    if (dragStack && dragStack.ids.indexOf(cardData.id) !== -1) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    e.stopPropagation();
    clearDropMarks();
    card.classList.add('card--drop');
  });
  card.addEventListener('dragleave', () => card.classList.remove('card--drop'));
  card.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation();
    clearDropMarks();
    const dragId = state.dragCardId || e.dataTransfer.getData('text/plain');
    const dragCard = dragId ? state.cards.find((n) => n.id === dragId) : null;
    if (dragCard && dragCard.status === 'draft') return;  // a draft drop links a dependency instead of stacking
    if (dragId) stackCards(dragId, cardData.id);
  });
}

/* The column background as a drop target: drop here to unstack a card. Wired
 * once per column at boot (renderBoard only replaces the column's children). */
function wireColumnDrop(wrap, colKey) {
  wrap.addEventListener('dragover', (e) => {
    if (!state.dragCardId || e.target.closest('.card')) return;
    const dragCard = state.cards.find((n) => n.id === state.dragCardId);
    if (!dragCard || cardColumn(dragCard) !== colKey) return;
    if (!findStackOf(state.dragCardId)) return;  // only stacked cards can leave a stack
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    wrap.classList.add('cards--drop');
  });
  wrap.addEventListener('dragleave', (e) => { if (e.target === wrap) wrap.classList.remove('cards--drop'); });
  wrap.addEventListener('drop', (e) => {
    if (e.target.closest('.card')) return;  // a card handled it
    e.preventDefault();
    wrap.classList.remove('cards--drop');
    if (state.dragCardId) unstackCard(state.dragCardId);
  });
}

/* Tilt of card `i` in a collapsed pile of `n`: a fan centred on the middle of
 * the stack, capped so a big pile doesn't splay across the column. */
const FAN_STEP_DEG = 2.4;
const FAN_MAX_DEG = 4;

function fanAngle(i, n) {
  if (n < 2) return 0;
  const half = (n - 1) / 2;
  const step = Math.min(FAN_STEP_DEG, FAN_MAX_DEG / half);
  return (i - half) * step;
}

/* A stack of cards: collapsed they lie on top of each other (small stagger +
 * count badge); hovered (.stack--open) they fan out vertically so each card is
 * visible and clickable. Layout uses --i (index), --n (count) and --card-h
 * (tallest card) custom properties; --card-h is measured after attach.
 *
 * The node returned is the wrapper (.stack-wrap): a header row carrying the
 * stack's name and its controls, above the pile itself (.stack). The pile keeps
 * the class name because the fan layout and the card lookups key off it. */
function buildStack(stack, byId, queuePos) {
  const wrap = el('div', 'stack-wrap');
  wrap.dataset.stackId = stack.id;
  const ids = stack.ids.filter((id) => byId.has(id));
  const n = ids.length;

  const head = el('div', 'stack-head');
  const title = el('span', 'stack-title', stack.title || '');
  if (!stack.title) title.classList.add('stack-title--empty');
  title.title = stack.title ? 'Click to rename this stack' : 'Click to name this stack';
  title.addEventListener('click', (e) => {
    e.stopPropagation();  // don't open a card editor behind the header
    startRenameStack(stack, title);
  });
  head.appendChild(title);
  if (queuePos) head.appendChild(buildQueuePosBadge(queuePos));
  wrap.appendChild(head);

  const pile = el('div', 'stack');
  let i = 0;
  for (const id of ids) {
    const cardData = byId.get(id);
    const card = buildCard(cardData);
    card.classList.add('stack-card');
    card.style.setProperty('--i', String(i));
    // Collapsed, the pile fans out like a hand of cards: each card tilts a
    // little more than the one below it, centred on the middle of the stack.
    card.style.setProperty('--fan', fanAngle(i, n).toFixed(2) + 'deg');
    i++;
    // Pull just this card out of the stack. Shown while the stack is fanned
    // out; dragging a card onto the column background still works too.
    const out = el('button', 'card-unstack icon-btn', '⤴');
    out.type = 'button';
    out.title = 'Unstack this card';
    out.addEventListener('click', (e) => {
      e.stopPropagation();  // don't open the editor
      unstackCard(cardData.id);
    });
    card.appendChild(out);
    wireCardDrag(card, cardData);
    wireCardDrop(card, cardData);
    pile.appendChild(card);
  }
  wrap.appendChild(pile);

  const count = el('span', 'stack-count', String(ids.length));
  count.title = ids.length + ' cards stacked — hover to separate them, ⤴ to unstack one or all';
  head.appendChild(count);
  // Unstacking must be obvious: one button dissolves the whole stack, and each
  // fanned card carries its own ⤴. Dragging a card onto the column background
  // used to be the only way, and nothing on screen said so.
  const unstack = el('button', 'stack-unstack icon-btn', '⤴');
  unstack.type = 'button';
  unstack.title = 'Unstack all ' + ids.length + ' cards';
  unstack.addEventListener('click', (e) => {
    e.stopPropagation();
    unstackAll(stack.id);
  });
  head.appendChild(unstack);
  // A stack in the accepted column is a group of accepted cards: offer archiving it whole.
  if (stack.col === 'accepted' && ids.length >= 2) {
    const arch = el('button', 'stack-archive icon-btn', '📦');
    arch.type = 'button';
    arch.title = 'Archive this whole stack into an archive';
    arch.addEventListener('click', (e) => {
      e.stopPropagation();
      const titles = ids.map((id) => byId.get(id).title);
      openArchives({ kind: 'cards', ids: ids.slice(), label: titles[0] + ' +' + (titles.length - 1) })
        .catch(showError);
    });
    head.appendChild(arch);
  }
  wrap.style.setProperty('--n', String(ids.length));
  if (state.openStackId === stack.id) wrap.classList.add('stack--open');
  wrap.addEventListener('mouseenter', () => {
    state.openStackId = stack.id;
    wrap.classList.add('stack--open');
  });
  wrap.addEventListener('mouseleave', () => {
    if (state.openStackId === stack.id) state.openStackId = null;
    wrap.classList.remove('stack--open');
  });
  return wrap;
}

/* Swap a stack's header for an input. Enter or blur commits, Escape cancels;
 * an empty name clears the header back to its placeholder. */
function startRenameStack(stack, titleEl) {
  if (titleEl.dataset.editing === '1') return;
  titleEl.dataset.editing = '1';
  const input = el('input', 'stack-title-input');
  input.type = 'text';
  input.value = stack.title || '';
  input.placeholder = 'Stack name';
  input.maxLength = 60;
  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    const next = input.value.trim();
    input.replaceWith(titleEl);
    delete titleEl.dataset.editing;
    if (commit && next !== (stack.title || '')) renameStack(stack.id, next);
  };
  input.addEventListener('click', (e) => e.stopPropagation());
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();  // the board's shortcuts must not fire while typing
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  titleEl.replaceWith(input);
  input.focus();
  input.select();
}

/* Measure the tallest card in the stack and feed it to the fan-out layout.
 * Called once the stack is attached (offsetHeight needs layout). */
function layoutStack(stackEl) {
  const cards = [...stackEl.querySelectorAll('.stack-card')];
  let maxH = 0;
  for (const c of cards) {
    // getComputedStyle().height is the layout (border-box) height: unlike
    // getBoundingClientRect() it ignores the collapsed pile's fan rotation, so
    // measuring it cannot feed the fan back into itself — that loop is what
    // shook the board.
    const h = parseFloat(getComputedStyle(c).height) || c.offsetHeight;
    maxH = Math.max(maxH, h);
  }
  if (maxH > 0) {
    maxH = Math.ceil(maxH);
    const v = maxH + 'px';
    // Write only on change: re-setting the same value still dirties style and
    // can retrigger the ResizeObserver (feedback loop -> shaking board).
    if (stackEl.style.getPropertyValue('--card-h') !== v) stackEl.style.setProperty('--card-h', v);
  }
  // A fanned pile is taller than one card: rotating a w×h card by θ grows its
  // box by ~w·sin θ in height. Reserve that (and shift the pile down by half of
  // it) so the rotated corners stay inside the stack box instead of painting
  // over the next card in the column.
  const w = stackEl.clientWidth;
  const maxAngle = cards.length > 1 ? Math.abs(fanAngle(0, cards.length)) : 0;
  const grow = w > 0 && maxAngle > 0 ? Math.ceil(w * Math.sin(maxAngle * Math.PI / 180)) + 1 : 0;
  const padV = grow + 'px';
  const shiftV = Math.ceil(grow / 2) + 'px';
  if (stackEl.style.getPropertyValue('--fan-pad') !== padV) stackEl.style.setProperty('--fan-pad', padV);
  if (stackEl.style.getPropertyValue('--fan-shift') !== shiftV) stackEl.style.setProperty('--fan-shift', shiftV);
  // The same rotation grows the box SIDEWAYS by ~h·sin θ (h = the card's own
  // height, not the pile's width). Without reserving it the fanned cards are
  // wider than the pile: they paint across the column's padding and their
  // shadow lands on the neighbouring column's cards — the pile "slightly
  // overlapping cards that aren't in the pile". Inset each card by half the
  // worst case, so the rotated box fits the pile exactly.
  //
  // Per card, not from the tallest one: the tallest card usually sits at angle
  // 0 (where rotation adds no width), so sizing the inset from it would shrink
  // the whole pile for nothing.
  let growX = 0;
  cards.forEach((c, i) => {
    const h = parseFloat(getComputedStyle(c).height) || c.offsetHeight;
    const angle = Math.abs(fanAngle(i, cards.length)) * Math.PI / 180;
    growX = Math.max(growX, h * Math.sin(angle));
  });
  const padXV = (growX > 0 ? Math.ceil(growX / 2) : 0) + 'px';
  if (stackEl.style.getPropertyValue('--fan-pad-x') !== padXV) stackEl.style.setProperty('--fan-pad-x', padXV);
}

/* ---------------- Search: the board and the archives ---------------- */

/* The board's cards are already in the browser (GET /api/cards carries every
 * body), so filtering the board is a plain scan of state.cards — no backend.
 * Archives load per folder, so finding an archived card by text asks the
 * server (GET /api/search); the results list under the input shows both,
 * grouped by where each card lives. */

const searchInput = $('search-input');
const searchHint = $('search-hint');
const searchResultsEl = $('search-results');

// A query typed quickly must not fire one request per keystroke.
const SEARCH_DEBOUNCE_MS = 150;

function searchNeedle() {
  return (state.searchQuery || '').trim().toLowerCase();
}

/* Does a card match the query? Title, body and id, case-insensitive — the same
 * fields the server searches in the archives. */
function cardMatchesQuery(card, needle) {
  if (!needle) return true;
  return (card.title || '').toLowerCase().indexOf(needle) !== -1
    || (card.body || '').toLowerCase().indexOf(needle) !== -1
    || (card.id || '').toLowerCase().indexOf(needle) !== -1;
}

/* "N of M cards" while a query is active: N match, M are on the board under
 * the project filter. */
function renderSearchHint() {
  const needle = searchNeedle();
  if (!needle) {
    searchHint.hidden = true;
    searchHint.textContent = '';
    return;
  }
  const shown = visibleCards().length;
  const total = projectCards().length;
  searchHint.hidden = false;
  searchHint.textContent = shown + ' of ' + total + (total === 1 ? ' card' : ' cards');
}

/* The server's hits grouped by location: Board first, then each archive in the
 * order the server listed them. */
function groupSearchResults(results) {
  const groups = [];
  const byWhere = new Map();
  for (const hit of results) {
    let group = byWhere.get(hit.where);
    if (!group) {
      group = { where: hit.where, hits: [] };
      byWhere.set(hit.where, group);
      groups.push(group);
    }
    group.hits.push(hit);
  }
  return groups.sort((a, b) => (b.where === 'board' ? 1 : 0) - (a.where === 'board' ? 1 : 0));
}

/* A snippet with the match highlighted: the server sends the text plus the
 * match's offsets, so nothing is searched twice. */
function buildSearchSnippet(snippet) {
  const p = el('p', 'search-snippet');
  const text = (snippet && snippet.text) || '';
  const start = snippet ? snippet.start : 0;
  const end = snippet ? snippet.end : 0;
  if (end > start) {
    p.appendChild(document.createTextNode(text.slice(0, start)));
    p.appendChild(el('mark', 'search-hit', text.slice(start, end)));
    p.appendChild(document.createTextNode(text.slice(end)));
  } else {
    p.textContent = text;
  }
  return p;
}

function renderSearchResults() {
  const needle = searchNeedle();
  searchResultsEl.textContent = '';
  if (!needle) {
    searchResultsEl.hidden = true;
    return;
  }
  searchResultsEl.hidden = false;
  if (state.searchPending) {
    searchResultsEl.appendChild(el('p', 'search-pending', 'Searching…'));
    return;
  }
  const groups = groupSearchResults(state.searchResults);
  if (!groups.length) {
    // An explicit empty state: a blank panel reads as "still loading".
    searchResultsEl.appendChild(el('p', 'search-empty',
      'No cards match “' + state.searchQuery.trim() + '” — not on the board, not in the archives.'));
    return;
  }
  for (const group of groups) {
    const box = el('div', 'search-group');
    box.appendChild(el('h4', 'search-group-head', group.where === 'board' ? 'Board' : group.where));
    for (const hit of group.hits) {
      const row = el('button', 'search-result');
      row.type = 'button';
      row.dataset.cardId = hit.id;
      row.dataset.where = hit.where;
      row.title = hit.where === 'board'
        ? 'Open this card'
        : 'Open the archive folder holding this card';
      const head = el('span', 'search-result-head');
      head.appendChild(el('span', 'search-result-title', hit.title || hit.id));
      if (hit.status) head.appendChild(chip('status-' + hit.status, hit.status));
      row.appendChild(head);
      row.appendChild(buildSearchSnippet(hit.snippet));
      row.addEventListener('click', () => openSearchResult(hit));
      box.appendChild(row);
    }
    searchResultsEl.appendChild(box);
  }
  if (state.searchTruncated) {
    searchResultsEl.appendChild(el('p', 'search-capped',
      'Showing the first ' + state.searchResults.length + ' of ' + state.searchTotal
      + ' matches — narrow the query to see the rest.'));
  }
}

/* How the board names an archive's location — mirrors app/search.py. */
function archiveWhere(a) {
  const project = (a.project || '').trim();
  const name = (a.name || '').trim();
  return project ? project + ' / ' + name : name;
}

/* A board hit opens the card's panel; an archived hit opens the archive folder
 * that holds it and highlights the card. */
function openSearchResult(hit) {
  if (hit.where === 'board') {
    openEditor(hit.id).catch(showError);
    return;
  }
  openArchivedResult(hit).catch(showError);
}

async function openArchivedResult(hit) {
  // The archive list is only loaded when a panel needs it; a search result can
  // be the first thing that needs it.
  if (!state.archives.length) await loadArchives(false);
  const archive = state.archives.find((a) => archiveWhere(a) === hit.where);
  if (!archive) throw new Error('no archive named ' + hit.where);
  // Unfold the tree down to the card: project header, then archive folder.
  state.sidebarProjectOpen.add(archive.project || '');
  state.sidebarArchiveOpen.add(archive.id);
  if (!state.archiveProjects.has(archive.id)) {
    const full = await api('/api/archives/' + encodeURIComponent(archive.id));
    state.archiveProjects.set(archive.id, full.projects || []);
  }
  await openSidebarPanel('archive', true);
  highlightArchivedCard(hit.id);
}

function highlightArchivedCard(cardId) {
  const row = sidebarBody.querySelector('.archive-item[data-card-id="' + CSS.escape(cardId) + '"]');
  if (!row) return;
  row.classList.add('archive-item--hit');
  if (row.scrollIntoView) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

/* Ask the server for the hits (board + archives). Debounced. */
function scheduleSearch() {
  if (state.searchTimer) clearTimeout(state.searchTimer);
  state.searchTimer = null;
  if (!searchNeedle()) {
    state.searchResults = [];
    state.searchTotal = 0;
    state.searchTruncated = false;
    state.searchPending = false;
    renderSearchResults();
    return;
  }
  state.searchPending = true;
  renderSearchResults();
  state.searchTimer = setTimeout(() => {
    state.searchTimer = null;
    runSearch().catch((err) => {
      // Never leave the panel stuck on "Searching…": the board filter still
      // works without the server, so fall back to an empty list plus the error.
      state.searchPending = false;
      state.searchResults = [];
      renderSearchResults();
      showError(err);
    });
  }, SEARCH_DEBOUNCE_MS);
}

async function runSearch() {
  const needle = searchNeedle();
  if (!needle) return;
  const res = await fetch('/api/search?q=' + encodeURIComponent(state.searchQuery.trim()));
  if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
  const results = await res.json();
  if (needle !== searchNeedle()) return;  // a newer query landed first
  state.searchResults = Array.isArray(results) ? results : [];
  const total = res.headers && res.headers.get ? res.headers.get('X-Search-Total') : null;
  state.searchTotal = total === null || total === undefined
    ? state.searchResults.length : Number(total);
  const capped = res.headers && res.headers.get ? res.headers.get('X-Search-Truncated') : null;
  state.searchTruncated = capped === 'true';
  state.searchPending = false;
  renderSearchResults();
}

/* The query changed: filter the board (local, instant) and re-ask the server
 * for the results list. */
function setSearchQuery(q) {
  state.searchQuery = q;
  renderBoard();      // filters the board and refreshes the "N of M cards" hint
  scheduleSearch();
}

function clearSearch() {
  if (state.searchTimer) { clearTimeout(state.searchTimer); state.searchTimer = null; }
  state.searchQuery = '';
  state.searchResults = [];
  state.searchTotal = 0;
  state.searchTruncated = false;
  state.searchPending = false;
  searchInput.value = '';
  for (const row of document.querySelectorAll('.archive-item--hit')) {
    row.classList.remove('archive-item--hit');
  }
  renderBoard();
  renderSearchResults();
}

searchInput.addEventListener('input', () => setSearchQuery(searchInput.value));

/* Cards under the current top-bar project filter ('' = all projects) and the
 * failure filter — the M in "N of M cards". */
function projectCards() {
  let cards = state.selectedProject === ''
    ? state.cards
    : state.cards.filter((n) => (n.project || '') === state.selectedProject);
  if (state.failureFilter) {
    // "show only failures of kind X": a card with no failure never matches.
    cards = cards.filter((n) => {
      const f = failureOf(n);
      return !!f && f.kind === state.failureFilter;
    });
  }
  return cards;
}

/* The cards the board shows: the project filter and the search both apply. */
function visibleCards() {
  const needle = searchNeedle();
  const cards = projectCards();
  return needle ? cards.filter((n) => cardMatchesQuery(n, needle)) : cards;
}

function renderBoard() {
  // While a drag is in progress the 2 s live poll must not tear the board
  // down: removing the dragged card's node mid-drag cancels the HTML5 drag in
  // the browser, so the drop (stack / dependency link) would never land.
  // Defer the rebuild until dragend flushes it.
  if (state.dragCardId) { state.renderPending = true; return; }
  state.renderPending = false;
  // The 2 s live poll re-renders the whole board (that is also what updates
  // the token count while a session runs). Tearing the columns down must not
  // yank the window back to the top, so the scroll offset is pinned across
  // the rebuild.
  const scrollX = window.scrollX;
  const scrollY = window.scrollY;
  cleanStacks();  // deleted / moved cards leave their stacks (server prunes too)
  let total = 0;
  const visible = visibleCards();
  for (const col of COLUMNS) {
    const wrap = $('col-' + col.key);
    // Keyed reuse: the 2 s live poll re-renders the board, but tearing down
    // unchanged cards recreates their DOM nodes — snapping in-flight CSS
    // transitions (stack fan-out, hover lift) and making stacks jump/shake.
    // Reuse the existing node when the item's render signature is unchanged
    // and only rebuild what actually changed.
    const oldNodes = new Map();
    for (const child of Array.from(wrap.children)) {
      const key = child.dataset && child.dataset.acceptAll ? 'accept-all'
        : child.dataset && child.dataset.stackId ? 'stack:' + child.dataset.stackId
        : child.dataset && child.dataset.cardId ? 'card:' + child.dataset.cardId : null;
      if (key) oldNodes.set(key, child);
    }
    const reused = new Set();
    const statuses = [col.key].concat(col.also || []);
    const cards = visible
      .filter((n) => statuses.indexOf(n.status) !== -1)
      .sort((a, b) => (parseTs(b.updated_at) || 0) - (parseTs(a.updated_at) || 0));
    const byId = new Map(cards.map((n) => [n.id, n]));
    // Stacks render as one slot, positioned where their newest member would
    // sort; unstacked cards render as plain cards.
    const stackItems = state.stacks
      .filter((s) => s.col === col.key && s.ids.some((id) => byId.has(id)))
      .map((s) => ({
        kind: 'stack',
        stack: s,
        ts: Math.max.apply(null, s.ids.filter((id) => byId.has(id)).map((id) => parseTs(byId.get(id).updated_at) || 0)),
      }));
    const stackedIds = new Set();
    for (const item of stackItems) for (const id of item.stack.ids) stackedIds.add(id);
    const cardItems = cards
      .filter((n) => !stackedIds.has(n.id))
      .map((n) => ({ kind: 'card', card: n, ts: parseTs(n.updated_at) || 0 }));
    let items = stackItems.concat(cardItems).sort((a, b) => b.ts - a.ts);
    // Done column: the "Accept all" button sits between the cards in the
    // running batch (above it) and the ones that finished afterwards (below
    // it), so a card that lands in Done mid-batch is visibly NOT included.
    let batchCount = 0, restCount = 0;
    if (col.key === 'done' && items.length) {
      const inBatch = (item) => item.kind === 'stack'
        ? item.stack.ids.some((id) => ((byId.get(id) || {}).session || {}).accept_queued)
        : !!((item.card.session || {}).accept_queued);
      const batch = items.filter(inBatch);
      const rest = items.filter((it) => !inBatch(it));
      // Number the batch in the order the drain will merge it: oldest
      // accept_queued_at first, id as the tie-break — the same rule the
      // server's _next_accept_queued() uses, so the numbers cannot lie.
      const queueKey = (item) => {
        const ids = item.kind === 'stack' ? item.stack.ids : [item.card.id];
        return ids.map((id) => ((byId.get(id) || {}).session || {}).accept_queued_at || '')
          .sort()[0] || '';
      };
      const firstId = (item) => (item.kind === 'stack' ? item.stack.ids[0] : item.card.id);
      batch.sort((a, b) => {
        const ka = queueKey(a), kb = queueKey(b);
        if (ka !== kb) return ka < kb ? -1 : 1;
        return firstId(a) < firstId(b) ? -1 : firstId(a) > firstId(b) ? 1 : 0;
      });
      batch.forEach((item, i) => { item.queuePos = i + 1; });
      batchCount = batch.length;
      restCount = rest.length;
      // No batch running: the button is simply the last thing in the column.
      // Batch running: it sits under the batch, with the cards that finished
      // afterwards below it.
      items = batchCount
        ? batch.concat([{ kind: 'accept-all', ts: 0 }], rest)
        : rest.concat([{ kind: 'accept-all', ts: 0 }]);
    }
    // Place the items in sorted order without needless DOM moves. appendChild
    // on a node that is already in position still removes and re-inserts it,
    // and a re-inserted node restarts its CSS transitions — so re-appending
    // every card on every 2 s poll made the whole board twitch (the "shaking"
    // board). Walk a cursor and only touch the DOM when a node is out of place.
    let cursor = wrap.firstElementChild;
    for (const item of items) {
      if (item.kind === 'accept-all') {
        const key = 'accept-all';
        const sig = 'accept-all#' + batchCount + '#' + restCount;
        const old = oldNodes.get(key);
        if (old && old._renderSig === sig) {
          reused.add(key);
          if (old !== cursor) wrap.insertBefore(old, cursor);
          cursor = old.nextElementSibling;
          continue;
        }
        if (old) {
          if (old === cursor) cursor = old.nextElementSibling;
          old.remove();
        }
        const btn = buildAcceptAll(batchCount, restCount);
        btn._renderSig = sig;
        if (cursor) wrap.insertBefore(btn, cursor); else wrap.appendChild(btn);
        cursor = btn.nextElementSibling;
        continue;
      }
      const key = item.kind === 'stack' ? 'stack:' + item.stack.id : 'card:' + item.card.id;
      // Everything the item renders from: a change rebuilds the node.
      const sig = item.kind === 'stack'
        ? item.stack.ids.join('|') + '#' + (item.stack.title || '') + '#' + item.stack.ids.map((id) => JSON.stringify(byId.get(id))).join('|') + '#q' + (item.queuePos || '')
        : JSON.stringify(item.card) + (state.expandedTitles.has(item.card.id) ? '#expanded' : '') + '#q' + (item.queuePos || '');
      const old = oldNodes.get(key);
      if (old && old._renderSig === sig) {
        reused.add(key);
        if (old !== cursor) wrap.insertBefore(old, cursor);  // only when out of place
        cursor = old.nextElementSibling;
        continue;
      }
      if (old) {
        // Keep the cursor attached: insertBefore needs a child of `wrap`.
        if (old === cursor) cursor = old.nextElementSibling;
        old.remove();
      }
      if (item.kind === 'stack') {
        const stackEl = buildStack(item.stack, byId, item.queuePos);
        stackEl._renderSig = sig;
        // The pile is the node that carries the fan layout (--card-h, --fan-pad,
        // --fan-shift) and the height transition; the wrapper only holds the
        // header above it.
        const pile = stackEl.querySelector('.stack');
        // A (re)built stack starts with the --card-h CSS fallback (140px) and
        // gets the measured value only after layoutStack runs below. Reading
        // layout in between (fitCardTitle) recalcs style at the fallback, so
        // the height transition would animate from the WRONG height on every
        // rebuild (every poll while a member is live) — the "permanent
        // shake". Suppress transitions until the first measurement is in.
        pile.style.transition = 'none';
        const stackCards = stackEl.querySelectorAll('.stack-card');
        stackCards.forEach((c) => { c.style.transition = 'none'; });
        if (cursor) wrap.insertBefore(stackEl, cursor); else wrap.appendChild(stackEl);
        cursor = stackEl.nextElementSibling;
        stackCards.forEach(fitCardTitle);
        layoutStack(pile);  // needs layout: the stack is attached by now
        requestAnimationFrame(() => {
          pile.style.transition = '';
          stackCards.forEach((c) => { c.style.transition = ''; });
        });
      } else {
        const card = buildCard(item.card, item.queuePos);
        card._renderSig = sig;
        wireCardDrag(card, item.card);
        wireCardDrop(card, item.card);
        if (cursor) wrap.insertBefore(card, cursor); else wrap.appendChild(card);
        cursor = card.nextElementSibling;
        fitCardTitle(card);  // needs layout: the card is attached by now
      }
    }
    // Drop the old nodes that were not reused (removed/moved cards).
    for (const [key, node] of oldNodes) if (!reused.has(key)) node.remove();
    $('count-' + col.key).textContent = String(cards.length);
    total += cards.length;
  }
  // An empty board is not always an empty board: a search that hides every
  // card, or a failure filter that matches none, says so instead.
  let emptyText = EMPTY_BOARD_TEXT;
  if (total === 0 && searchNeedle()) {
    emptyText = 'No cards on the board match “' + state.searchQuery.trim() + '”.';
  } else if (total === 0 && state.failureFilter) {
    emptyText = 'No card failed that way — pick “any reason” to see the whole board.';
  }
  if (emptyBoard.textContent !== emptyText) emptyBoard.textContent = emptyText;
  emptyBoard.hidden = total > 0;
  renderStrings();
  renderSearchHint();  // the project filter changes the M in "N of M cards"
  window.scrollTo(scrollX, scrollY);
}

/* ---------------- Red strings (dependency links between cards) ---------------- */

const stringsLayer = $('strings-layer');
const SVG_NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}

/* Top-center anchor of a card, in the board's local (scroll-safe) coords. */
function cardAnchor(card, boardRect) {
  const r = card.getBoundingClientRect();
  return { x: r.left - boardRect.left + r.width / 2, y: r.top - boardRect.top + 6 };
}

/* Draw a red string from each dependent card to the card it runs after (the
 * arrowhead points at the card that runs first — a queued draft points at the
 * running/reviewing card it waits for), like yarn on a corkboard.
 * Recomputed on every board render and on resize. */
/* Scroll a card into view and flash it, so a parent/child chip actually takes
 * you somewhere. The card may be in another column or inside a stack. */
function jumpToCard(cardId) {
  const card = document.querySelector('.card[data-card-id="' + CSS.escape(cardId) + '"]');
  if (!card) return;
  card.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
  card.classList.add('card--flash');
  setTimeout(() => card.classList.remove('card--flash'), 1200);
}

function renderStrings() {
  if (!stringsLayer) return;
  const board = $('board');
  stringsLayer.textContent = '';
  const defs = svgEl('defs', {});
  const marker = svgEl('marker', {
    id: 'string-arrow', viewBox: '0 0 10 10', refX: '9', refY: '5',
    markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse',
  });
  marker.appendChild(svgEl('path', { d: 'M 0 1 L 9 5 L 0 9 z', fill: '#d62728' }));
  defs.appendChild(marker);
  stringsLayer.appendChild(defs);

  // Size the SVG from the board's in-flow content only — NOT scrollWidth/
  // scrollHeight: this absolutely-positioned SVG itself contributes to the
  // board's scrollable overflow, so sizing it from scrollHeight would feed
  // its own height back (page height would only ever grow, freezing dead
  // scrollable space at the bottom).
  let contentW = 0, contentH = 0;
  for (const child of board.children) {
    if (child === stringsLayer) continue;
    contentW = Math.max(contentW, child.offsetLeft + child.offsetWidth);
    contentH = Math.max(contentH, child.offsetTop + child.offsetHeight);
  }
  const svgW = String(Math.max(contentW, board.clientWidth));
  const svgH = String(Math.max(contentH, board.clientHeight));
  if (stringsLayer.getAttribute('width') !== svgW) stringsLayer.setAttribute('width', svgW);
  if (stringsLayer.getAttribute('height') !== svgH) stringsLayer.setAttribute('height', svgH);

  const boardRect = board.getBoundingClientRect();
  const visible = visibleCards();
  const visIds = new Set(visible.map((n) => n.id));
  const statusIndex = new Map(state.cards.map((n) => [n.id, n]));
  const cardOf = (id) => board.querySelector('.card[data-card-id="' + CSS.escape(id) + '"]');
  for (const n of visible) {
    // Only the links still being waited on: once the dependency is merged the
    // string has done its job and is not drawn any more.
    for (const dep of unmetDeps(n, statusIndex)) {
      if (!visIds.has(dep)) continue;  // dependency not visible in this project view
      const fromCard = cardOf(n.id);   // the dependent (the queued draft)
      const toCard = cardOf(dep);      // the dependency (runs first)
      if (!fromCard || !toCard) continue;
      const a = cardAnchor(fromCard, boardRect);
      const b = cardAnchor(toCard, boardRect);
      // Sagging string: quadratic curve with the control point below the midpoint.
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2 + Math.min(70, 24 + Math.abs(a.x - b.x) / 5);
      stringsLayer.appendChild(svgEl('path', {
        d: 'M ' + a.x + ' ' + a.y + ' Q ' + mx + ' ' + my + ' ' + b.x + ' ' + b.y,
        'class': 'string-line',
        'marker-end': 'url(#string-arrow)',
      }));
      stringsLayer.appendChild(svgEl('circle', { cx: a.x, cy: a.y, r: 3.5, 'class': 'string-pin' }));
    }
  }
}

$('restart-server-btn').addEventListener('click', async () => {
  const btn = $('restart-server-btn');
  btn.disabled = true;
  try {
    await api('/api/server/restart', { method: 'POST' });
    // The server is killed right after responding; poll until it is back.
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      try { await api('/api/cards'); break; }
      catch (_) { await new Promise(r => setTimeout(r, 1000)); }
    }
    await refresh();
  } catch (err) { showError(err); }
  finally { btn.disabled = false; }
});
/* ---------------- Project picker ---------------- */

/* The last entry of the project menu: a command, not a project. Picking it
 * opens the projects modal (add / remove), so the top bar needs no separate
 * "＋ Project" button. The sentinel value is not a name anyone registers a
 * project under. */
const ADD_PROJECT_VALUE = '__add_project__';

async function loadProjects() {
  try {
    state.projects = await api('/api/projects');
  } catch (err) {
    showError(err);
    state.projects = [];
  }
  const previous = state.selectedProject;
  projectSelect.textContent = '';
  const all = el('option', '', 'All projects');
  all.value = '';
  projectSelect.appendChild(all);
  for (const p of state.projects) {
    const opt = el('option', '', p.name);
    opt.value = p.name;
    projectSelect.appendChild(opt);
  }
  // "Add project…" sits at the BOTTOM of the menu, after every project.
  const addProject = el('option', '', '＋ Add project…');
  addProject.value = ADD_PROJECT_VALUE;
  projectSelect.appendChild(addProject);
  // Keep the selection if the project still exists. A remembered selection
  // whose project was since deleted falls back to All projects, and the stale
  // memory is overwritten with that fallback.
  const stillThere = state.projects.some((p) => p.name === previous);
  state.selectedProject = previous !== '' && stillThere ? previous : '';
  saveSelectedProject(state.selectedProject);
  projectSelect.value = state.selectedProject;

  // Keep the editor's project dropdown in sync: "— no project —" + each
  // registered project. Preserve the editor's current value across rebuilds.
  const editorValue = cardProject.value;
  cardProject.textContent = '';
  const none = el('option', '', '— no project —');
  none.value = '';
  cardProject.appendChild(none);
  for (const p of state.projects) {
    const opt = el('option', '', p.name);
    opt.value = p.name;
    cardProject.appendChild(opt);
  }
  if (editorValue !== '' && !state.projects.some((p) => p.name === editorValue)) {
    // Keep an unregistered label the editor is showing (e.g. a card whose
    // project was deleted while the editor is open).
    const opt = el('option', '', editorValue);
    opt.value = editorValue;
    cardProject.appendChild(opt);
  }
  cardProject.value = editorValue;

  // The archive form's project picker: same list, defaulting to the project
  // the board is filtered to (that is the one the user is looking at).
  const archiveValue = archiveProjectInput.value || state.selectedProject || '';
  archiveProjectInput.textContent = '';
  const archiveNone = el('option', '', '— no project —');
  archiveNone.value = '';
  archiveProjectInput.appendChild(archiveNone);
  for (const p of state.projects) {
    const opt = el('option', '', p.name);
    opt.value = p.name;
    archiveProjectInput.appendChild(opt);
  }
  if (archiveValue !== '' && !state.projects.some((p) => p.name === archiveValue)) {
    const opt = el('option', '', archiveValue);
    opt.value = archiveValue;
    archiveProjectInput.appendChild(opt);
  }
  archiveProjectInput.value = archiveValue;
}

/* Make sure the editor's project dropdown offers `name`, even when it is not a
 * registered project (a card can keep the label of a since-deleted project). */
function ensureProjectOption(name) {
  if (!name) return;
  if (!state.projects.some((p) => p.name === name)
      && !Array.from(cardProject.options).some((o) => o.value === name)) {
    const opt = el('option', '', name);
    opt.value = name;
    cardProject.appendChild(opt);
  }
}

projectSelect.addEventListener('change', () => {
  // "Add project…" is a command: open the projects modal and put the menu back
  // on the project the board is filtered to (the pick itself is not a filter).
  if (projectSelect.value === ADD_PROJECT_VALUE) {
    projectSelect.value = state.selectedProject;
    openProjects().catch(showError);
    return;
  }
  state.selectedProject = projectSelect.value;
  // Remember the selection for the next browser session…
  saveSelectedProject(state.selectedProject);
  // …and working on a concrete project here makes it the last used one, so
  // the next new card defaults to it. ("All projects" is a view filter, not a
  // project use, and does not touch the last-used memory.)
  if (state.selectedProject) saveLastProjectDefault(state.selectedProject);
  refresh();
});

/* The failure filter: narrow the board to one reason a card can fail for. */
$('failure-filter').addEventListener('change', (e) => {
  state.failureFilter = e.target.value || '';
  saveFailureFilter(state.failureFilter);
  renderBoard();
});

/* ---------------- Projects modal ---------------- */

function renderProjectList() {
  projectListItems.textContent = '';
  if (state.projects.length === 0) {
    projectListItems.appendChild(el('li', 'project-empty', 'No projects yet — add one above.'));
    return;
  }
  for (const p of state.projects) {
    const li = el('li');
    li.appendChild(el('span', 'pl-name', p.name));
    li.appendChild(el('span', 'pl-path', p.path));
    const del = el('button', 'btn btn-danger', 'Delete');
    del.type = 'button';
    del.addEventListener('click', () => action(async () => {
      await api('/api/projects/' + encodeURIComponent(p.name), { method: 'DELETE' });
      await loadProjects();
    }));
    li.appendChild(del);
    projectListItems.appendChild(li);
  }
}

async function openProjects() {
  await loadProjects();
  renderProjectList();
  projectsOverlay.hidden = false;
  projectNameInput.focus();
}

function closeProjects() {
  projectsOverlay.hidden = true;
}

$('projects-close').addEventListener('click', closeProjects);
projectsOverlay.addEventListener('click', (e) => { if (e.target === projectsOverlay) closeProjects(); });

// The native folder picker is a macOS dialog run by the server; the browser and
// server are the same machine for this local tool, so the browser's platform is
// the server's platform. Off macOS, hide Browse… and keep typing the path.
const pickerPlatform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
if (!/mac/i.test(pickerPlatform)) projectBrowseBtn.hidden = true;

projectBrowseBtn.addEventListener('click', () => action(async () => {
  projectBrowseBtn.disabled = true;
  try {
    const res = await fetch('/api/fs/choose-folder', { method: 'POST' });
    if (res.ok) {
      projectPathInput.value = (await res.json()).path;
      if (!projectNameInput.value.trim()) projectNameInput.focus();
    } else if (res.status === 409) {
      /* cancelled — keep whatever is typed */
    } else {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'Folder picker unavailable — type the path instead.');
    }
  } finally {
    projectBrowseBtn.disabled = false;
  }
}));

projectForm.addEventListener('submit', (e) => {
  e.preventDefault();
  action(async () => {
    const name = projectNameInput.value.trim();
    const path = projectPathInput.value.trim();
    if (!name || !path) throw new Error('Name and path are required.');
    await api('/api/projects', { method: 'POST', body: JSON.stringify({ name: name, path: path }) });
    projectNameInput.value = '';
    projectPathInput.value = '';
    await loadProjects();
    renderProjectList();
  });
});

/* ---------------- Archives modal ---------------- */

/* The archive is a folder structure (archives/<slug>-<id>/ with archive.md +
 * cards/); here the user creates typed archives and moves accepted cards,
 * whole stacks and whole projects into them. */

async function loadArchives(withItems) {
  try {
    state.archives = await api('/api/archives');
    if (withItems) {
      for (const a of state.archives) {
        try {
          const full = await api('/api/archives/' + encodeURIComponent(a.id));
          a.items = full.items || [];
          a.projects = full.projects || [];
        } catch (_) { a.items = []; a.projects = []; }
      }
    }
  } catch (err) {
    showError(err);
    state.archives = [];
  }
}

function acceptedBoardCards() {
  return state.cards.filter((n) => n.status === 'accepted');
}

/* Stacks in the accepted column whose cards all still exist. */
function acceptedStacks() {
  const byId = new Map(state.cards.map((n) => [n.id, n]));
  const out = [];
  for (const s of state.stacks) {
    if (s.col !== 'accepted') continue;
    const ids = s.ids.filter((id) => byId.has(id) && byId.get(id).status === 'accepted');
    if (ids.length < 2) continue;
    const names = ids.map((id) => byId.get(id).title);
    out.push({ id: s.id, ids, label: names[0] + (names.length > 1 ? ' +' + (names.length - 1) : '') });
  }
  return out;
}

function archivePickLabel(pick) {
  if (!pick) return '';
  if (pick.kind === 'card') return 'card “' + pick.label + '”';
  if (pick.kind === 'cards') return 'stack “' + pick.label + '” (' + pick.ids.length + ' cards)';
  return 'project “' + pick.label + '”';
}

async function movePickIntoArchive(archiveId) {
  const pick = state.archivePick;
  if (pick.kind === 'card') {
    await api('/api/archives/' + encodeURIComponent(archiveId) + '/cards/' + encodeURIComponent(pick.id), { method: 'POST' });
  } else if (pick.kind === 'cards') {
    await api('/api/archives/' + encodeURIComponent(archiveId) + '/cards', {
      method: 'POST', body: JSON.stringify({ card_ids: pick.ids }),
    });
  } else if (pick.kind === 'project') {
    await api('/api/archives/' + encodeURIComponent(archiveId) + '/projects/' + encodeURIComponent(pick.name), { method: 'POST' });
  }
}

function renderArchiveList() {
  archiveListItems.textContent = '';
  const pick = state.archivePick;
  archivePickBanner.hidden = !pick;
  if (pick) archivePickBanner.textContent = 'Choose an archive for ' + archivePickLabel(pick) + ' — it will be moved there:';
  if (state.archives.length === 0) {
    archiveListItems.appendChild(el('li', 'project-empty',
      'No archives yet — create one above. The archive is a folder under archives/ per archive.'));
    return;
  }
  for (const a of state.archives) {
    const li = el('li', 'archive-item');
    const head = el('div', 'archive-head');
    head.appendChild(el('span', 'pl-name', a.name));
    head.appendChild(chip('archive-type', a.type || 'cards'));
    head.appendChild(el('span', 'pl-path', a.item_count + (a.item_count === 1 ? ' card' : ' cards')
      + (a.archived_projects && a.archived_projects.length ? ' · projects: ' + a.archived_projects.map((d) => d.name).join(', ') : '')));
    if (pick) {
      const move = el('button', 'btn btn-primary', 'Move here');
      move.type = 'button';
      move.addEventListener('click', () => action(async () => {
        await movePickIntoArchive(a.id);
        if (pick.kind === 'card' && pick.id === state.editorCardId) closeEditor();
        closeArchives();
        await refresh();
      }));
      head.appendChild(move);
    } else {
      const toggle = el('button', 'btn', state.archiveExpanded.has(a.id) ? 'Cards ▴' : 'Cards ▾');
      toggle.type = 'button';
      toggle.title = 'Show the archived cards';
      toggle.addEventListener('click', async () => {
        if (state.archiveExpanded.has(a.id)) state.archiveExpanded.delete(a.id);
        else {
          state.archiveExpanded.add(a.id);
          try {
            const full = await api('/api/archives/' + encodeURIComponent(a.id));
            a.items = full.items || [];
            a.projects = full.projects || [];
          } catch (err) { showError(err); }
        }
        renderArchiveList();
      });
      head.appendChild(toggle);
    }
    li.appendChild(head);

    if (state.archiveExpanded.has(a.id) && !pick) {
      const items = el('ul', 'archive-cards');
      const groups = a.projects || [];
      if (groups.length === 0) {
        items.appendChild(el('li', 'project-empty', 'Empty — archive accepted cards into it below.'));
      }
      // The archive is project-related: its cards are listed under a project
      // header each (the same grouping the sidebar's archive tree shows).
      for (const g of groups) {
        items.appendChild(el('li', 'archive-project-title',
          (g.name || 'No project') + ' · ' + (g.count || 0)));
        for (const n of (g.cards || [])) {
          const row = el('li', 'archive-card');
          row.appendChild(el('span', 'pl-name', n.title));
          row.appendChild(chip('status-' + n.status, n.status));
          const restore = el('button', 'btn', 'Restore to board');
          restore.type = 'button';
          restore.title = 'Move this card back to the board (it reappears in Accepted)';
          restore.addEventListener('click', () => action(async () => {
            await api('/api/archives/' + encodeURIComponent(a.id) + '/cards/' + encodeURIComponent(n.id), { method: 'DELETE' });
            await loadArchives(true);
            renderArchiveList();
            await refresh();
          }));
          row.appendChild(restore);
          items.appendChild(row);
        }
      }
      li.appendChild(items);

      // Add controls: an accepted card, a whole stack, or a whole project.
      const controls = el('div', 'archive-controls');
      const cardRow = el('span', 'path-row');
      const cardSelect = el('select');
      const accepted = acceptedBoardCards();
      if (accepted.length === 0) {
        cardSelect.appendChild(el('option', '', 'no accepted cards on the board'));
        cardSelect.disabled = true;
      } else {
        for (const n of accepted) {
          const opt = el('option', '', n.title + ' (' + n.id + ')');
          opt.value = n.id;
          cardSelect.appendChild(opt);
        }
      }
      const cardBtn = el('button', 'btn', 'Archive card');
      cardBtn.type = 'button';
      cardBtn.disabled = accepted.length === 0;
      cardBtn.addEventListener('click', () => action(async () => {
        await api('/api/archives/' + encodeURIComponent(a.id) + '/cards/' + encodeURIComponent(cardSelect.value), { method: 'POST' });
        await loadArchives(true);
        renderArchiveList();
        await refresh();
      }));
      cardRow.appendChild(cardSelect);
      cardRow.appendChild(cardBtn);
      controls.appendChild(cardRow);

      const stacks = acceptedStacks();
      if (stacks.length) {
        const stackRow = el('span', 'path-row');
        const stackSelect = el('select');
        for (const s of stacks) {
          const opt = el('option', '', 'stack: ' + s.label);
          opt.value = s.id;
          stackSelect.appendChild(opt);
        }
        const stackBtn = el('button', 'btn', 'Archive stack');
        stackBtn.type = 'button';
        stackBtn.title = 'Archive the whole stack (all its accepted cards) into this archive';
        stackBtn.addEventListener('click', () => action(async () => {
          const s = stacks.find((x) => x.id === stackSelect.value);
          await api('/api/archives/' + encodeURIComponent(a.id) + '/cards', {
            method: 'POST', body: JSON.stringify({ card_ids: s.ids }),
          });
          await loadArchives(true);
          renderArchiveList();
          await refresh();
        }));
        stackRow.appendChild(stackSelect);
        stackRow.appendChild(stackBtn);
        controls.appendChild(stackRow);
      }

      if (state.projects.length) {
        const projRow = el('span', 'path-row');
        const projSelect = el('select');
        for (const p of state.projects) {
          const opt = el('option', '', p.name);
          opt.value = p.name;
          projSelect.appendChild(opt);
        }
        const projBtn = el('button', 'btn btn-danger', 'Archive project');
        projBtn.type = 'button';
        projBtn.title = 'Archive the entire project: its cards move into this archive and the project is unregistered';
        projBtn.addEventListener('click', () => action(async () => {
          const name = projSelect.value;
          if (!window.confirm('Archive the whole project “' + name + '”? Its cards move into this archive and the project is unregistered.')) return;
          await api('/api/archives/' + encodeURIComponent(a.id) + '/projects/' + encodeURIComponent(name), { method: 'POST' });
          await loadProjects();
          await loadArchives(true);
          renderArchiveList();
          await refresh();
        }));
        projRow.appendChild(projSelect);
        projRow.appendChild(projBtn);
        controls.appendChild(projRow);
      }
      li.appendChild(controls);
    }
    archiveListItems.appendChild(li);
  }
}

async function openArchives(pick) {
  state.archivePick = pick || null;
  await loadArchives(true);
  renderArchiveList();
  archivesOverlay.hidden = false;
  if (!pick) archiveNameInput.focus();
}

function closeArchives() {
  archivesOverlay.hidden = true;
  state.archivePick = null;
}

archivesBtn.addEventListener('click', () => { openArchives(null).catch(showError); });
$('archives-close').addEventListener('click', closeArchives);
archivesOverlay.addEventListener('click', (e) => { if (e.target === archivesOverlay) closeArchives(); });

archiveForm.addEventListener('submit', (e) => {
  e.preventDefault();
  action(async () => {
    const name = archiveNameInput.value.trim();
    if (!name) throw new Error('Archive name is required.');
    const type = archiveTypeInput.value.trim() || 'cards';
    const project = archiveProjectInput.value || '';
    await api('/api/archives', { method: 'POST',
      body: JSON.stringify({ name: name, type: type, project: project }) });
    archiveNameInput.value = '';
    archiveTypeInput.value = '';
    await loadArchives(true);
    renderArchiveList();
  });
});

/* ---------------- Sidebar ---------------- */

/* A rail of buttons on the left; clicking one opens its panel next to the rail
 * and clicking it again closes it. One panel at a time. */
const sidebarPanel = $('sidebar-panel');
const sidebarTitle = $('sidebar-title');
const sidebarBody = $('sidebar-body');
const sidebarRail = $('sidebar-rail');

const SIDEBAR_PANELS = {
  settings: { title: 'Settings', render: renderSettingsPanel },
  models: { title: 'Models', render: renderModelsPanel },
  archive: { title: 'Archive', render: renderArchivePanel },
  split: { title: 'Split', render: renderSplitPanel },
  keys: { title: 'Keys', render: renderKeysPanel },
};

function openSidebarPanel(key, force) {
  const panel = SIDEBAR_PANELS[key];
  if (!panel) return;
  // `force` opens the panel even when it is already showing — a search result
  // must lead INTO the archive browser, not toggle it shut.
  if (!force && state.sidebarPanel === key && !sidebarPanel.hidden) { closeSidebar(); return; }
  state.sidebarPanel = key;
  sidebarTitle.textContent = panel.title;
  sidebarPanel.hidden = false;
  for (const btn of sidebarRail.querySelectorAll('.rail-btn')) {
    btn.setAttribute('aria-expanded', btn.dataset.panel === key ? 'true' : 'false');
  }
  return panel.render().catch(showError);
}

function closeSidebar() {
  state.sidebarPanel = null;
  sidebarPanel.hidden = true;
  for (const btn of sidebarRail.querySelectorAll('.rail-btn')) btn.setAttribute('aria-expanded', 'false');
}

for (const btn of sidebarRail.querySelectorAll('.rail-btn')) {
  btn.addEventListener('click', () => openSidebarPanel(btn.dataset.panel));
}
$('sidebar-close').addEventListener('click', closeSidebar);

/* --- keys: every keyboard shortcut, generated from KEYBINDINGS --- */

async function renderKeysPanel() {
  sidebarBody.textContent = '';
  sidebarBody.appendChild(el('p', 'field-hint',
    'Shortcuts work anywhere on the board (not while typing in a field).'));
  const list = el('ul', 'keys-list');
  for (const binding of KEYBINDINGS) {
    const row = el('li', 'keys-row');
    row.appendChild(el('kbd', 'keys-key', binding.key));
    const text = el('div', 'keys-text');
    text.appendChild(el('strong', 'keys-label', binding.label));
    text.appendChild(el('span', 'keys-hint', binding.hint));
    row.appendChild(text);
    list.appendChild(row);
  }
  sidebarBody.appendChild(list);
}

/* --- settings: the concurrency cap and browser-side preferences --- */

/* The concurrency cap. The runner reads it per run, so a change applies to the
 * next Run click without a restart; cards over the cap wait in Draft and start
 * automatically, oldest queue time first, as sessions end. */
async function renderSettingsPanel() {
  const settings = await api('/api/settings');
  state.settings = settings;
  sidebarBody.textContent = '';

  const row = el('div', 'model-role');
  const label = el('label', '', 'Max concurrent sessions');
  label.htmlFor = 'max-concurrent-input';
  row.appendChild(label);

  const input = el('input');
  input.type = 'number';
  input.id = 'max-concurrent-input';
  input.min = '0';
  input.step = '1';
  input.value = String(settings.max_concurrent);
  input.title = 'How many agent sessions may run at once — 0 = unlimited';
  input.addEventListener('change', () => {
    saveMaxConcurrent(input.value).catch(showError);
  });
  row.appendChild(input);
  row.appendChild(el('p', 'model-hint',
    'How many agent sessions may run at once (0 = unlimited). Cards over the '
    + 'cap wait in Draft and start automatically, oldest first, as sessions end.'));
  sidebarBody.appendChild(row);

  const running = settings.running || 0;
  const limit = settings.max_concurrent;
  const occupancy = el('p', 'sidebar-empty', limit > 0
    ? running + ' of ' + limit + ' running'
    : running + ' running (no limit)');
  occupancy.id = 'max-concurrent-occupancy';
  sidebarBody.appendChild(occupancy);

  /* Browser-side preferences: these describe what this tab does, not how the
   * harness runs sessions, so they live in localStorage, not settings.json. */
  const notifyRow = el('label', 'setting-row');
  const box = el('input');
  box.type = 'checkbox';
  box.id = 'notify-toggle';
  box.checked = state.notificationsEnabled;
  box.title = 'Show a desktop notification when a card finishes while you are in another window';
  box.addEventListener('change', () => setNotificationsEnabled(box.checked));
  notifyRow.appendChild(box);
  notifyRow.appendChild(el('span', 'setting-label', 'Desktop notifications'));
  sidebarBody.appendChild(notifyRow);

  const permission = notificationPermission();
  sidebarBody.appendChild(el('p', 'sidebar-empty',
    permission === 'denied'
      ? 'This browser blocks notifications for the dashboard, so a short beep plays instead. '
        + 'Allow notifications in the site settings to get the desktop popup back.'
      : permission === 'unsupported'
        ? 'This browser has no Notification API, so a short beep plays instead.'
        : 'A card that finishes while you are in another window pops up a desktop notification. '
          + 'The browser asks for permission on your first click.'));

  /* Sleep guard: the server holds an idle-sleep inhibitor while cards run or
   * wait. A sleeping machine suspends the session process and the CLI reports
   * `interrupted` — a card that looks failed but was only paused. */
  const sleepRow = el('label', 'setting-row');
  const sleepBox = el('input');
  sleepBox.type = 'checkbox';
  sleepBox.id = 'prevent-sleep-toggle';
  sleepBox.checked = !!settings.prevent_sleep;
  sleepBox.disabled = !settings.sleep_supported;
  sleepBox.title = 'Keep the machine awake while cards run or wait';
  sleepBox.addEventListener('change',
    () => savePreventSleep(sleepBox.checked).catch(showError));
  sleepRow.appendChild(sleepBox);
  sleepRow.appendChild(el('span', 'setting-label',
    'Keep the computer awake while cards run'));
  sidebarBody.appendChild(sleepRow);

  sidebarBody.appendChild(el('p', 'sidebar-empty',
    !settings.sleep_supported
      ? 'This platform has no idle-sleep inhibitor the harness can hold, so the '
        + 'machine may sleep mid-run and the card comes back as "interrupted".'
      : settings.sleep_held
        ? 'Holding the machine awake right now — a card is running or waiting.'
        : 'While a card is running or queued, the harness stops the machine from '
          + 'going to sleep. A sleeping machine suspends the session and the card '
          + 'comes back as "interrupted".'));
  if (settings.sleep_supported) {
    sidebarBody.appendChild(el('p', 'sidebar-empty',
      'Closing the lid still sleeps the machine — no userspace process can '
      + 'override that. Only `sudo pmset -a disablesleep 1` does, and it is a '
      + 'system-wide root setting the harness will not touch.'));
  }
}

async function savePreventSleep(on) {
  state.settings = await api('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ prevent_sleep: !!on }),
  });
  await renderSettingsPanel();
}

async function saveMaxConcurrent(raw) {
  const value = Math.max(0, Math.floor(Number(raw) || 0));
  state.settings = await api('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ max_concurrent: value }),
  });
  await renderSettingsPanel();
}

/* --- models: which model each kind of session runs on --- */

/* Which backend runs sessions, cached for the stats bar and the card chips. */
async function loadSettings() {
  try {
    state.settings = await api('/api/settings');
  } catch (_) {
    state.settings = null;  // assume the default backend
  }
  // The failure filter is built from the classifier's kinds, which arrive with
  // the settings: repaint it now that they are known (the first paint used the
  // fallback list).
  renderFailureFilter();
  return state.settings;
}

/* The pickable models, cached for the per-card pickers. */
async function loadModels() {
  try {
    state.models = await api('/api/models');
  } catch (_) {
    state.models = null;  // the card pickers fall back to "inherit" only
  }
  return state.models;
}

async function renderModelsPanel() {
  const [data, settings] = await Promise.all([api('/api/models'), api('/api/settings')]);
  state.models = data;
  state.settings = settings;
  sidebarBody.textContent = '';

  // Which agent CLI the harness drives. This sits above the per-role models
  // because it decides what those models even mean.
  const backendRow = el('div', 'model-role backend-role');
  const backendLabel = el('label', '', 'Backend');
  backendLabel.htmlFor = 'backend-select';
  backendRow.appendChild(backendLabel);
  const backendSelect = el('select');
  backendSelect.id = 'backend-select';
  for (const b of settings.backends) {
    const opt = el('option', '', b.label + (b.available ? '' : ' — not installed'));
    opt.value = b.key;
    opt.disabled = !b.available;
    backendSelect.appendChild(opt);
  }
  backendSelect.value = settings.backend;
  backendSelect.title = 'Which agent CLI the harness spawns for every session';
  backendSelect.addEventListener('change', () => {
    saveBackend(backendSelect.value).catch(showError);
  });
  backendRow.appendChild(backendSelect);
  const chosen = settings.backends.find((b) => b.key === settings.backend);
  backendRow.appendChild(el('p', 'model-hint',
    chosen && chosen.available
      ? 'Sessions run on ' + chosen.label + ' (' + chosen.binary + ').'
      : 'This backend is not installed — runs will fail until ' + (chosen ? chosen.binary : 'it') + ' is on PATH.'));
  sidebarBody.appendChild(backendRow);

  // How accepting a card ships the change: merge locally, or open a pull
  // request (push the branch, verdict as the PR body). It sits next to the
  // backend because it decides what the Accept button even does.
  const acceptRow = el('div', 'model-role backend-role');
  const acceptLabel = el('label', '', 'Accept mode');
  acceptLabel.htmlFor = 'accept-mode-select';
  acceptRow.appendChild(acceptLabel);
  const acceptSelect = el('select');
  acceptSelect.id = 'accept-mode-select';
  for (const m of settings.accept_modes) {
    const opt = el('option', '', m.label);
    opt.value = m.key;
    acceptSelect.appendChild(opt);
  }
  acceptSelect.value = settings.accept_mode;
  acceptSelect.title = 'How accepting a card ships the change';
  acceptSelect.addEventListener('change', () => {
    saveAcceptMode(acceptSelect.value).catch(showError);
  });
  acceptRow.appendChild(acceptSelect);
  const chosenMode = settings.accept_modes.find((m) => m.key === settings.accept_mode);
  acceptRow.appendChild(el('p', 'model-hint', chosenMode ? chosenMode.description : ''));
  sidebarBody.appendChild(acceptRow);

  for (const role of data.roles) {
    const row = el('div', 'model-role' + (role.available ? '' : ' model-role--off'));
    const id = 'model-' + role.key;
    const label = el('label', '', role.label);
    label.htmlFor = id;
    row.appendChild(label);

    const select = el('select');
    select.id = id;
    const inherit = el('option', '', '— inherit (' + (data.default_model || 'config') + ') —');
    inherit.value = '';
    select.appendChild(inherit);
    for (const m of data.models) {
      const opt = el('option', '', m.display_name + (m.thinking ? ' · ' + m.thinking : ''));
      opt.value = m.alias;
      select.appendChild(opt);
    }
    select.value = role.model || '';
    select.disabled = !role.available;
    select.title = role.description;
    select.addEventListener('change', () => {
      saveRoleModel(role.key, select.value).catch(showError);
    });
    row.appendChild(select);
    row.appendChild(el('p', 'model-hint', role.description));
    sidebarBody.appendChild(row);
  }
  sidebarBody.appendChild(el('p', 'sidebar-empty',
    'A role left on “inherit” runs on your own active_model from ~/.vibe/config.toml. '
    + 'A chosen model is pinned for that session only (VIBE_ACTIVE_MODEL), so the global '
    + 'config is never rewritten and two sessions cannot race each other.'));
}

async function saveBackend(key) {
  state.settings = await api('/api/settings', {
    method: 'PUT', body: JSON.stringify({ backend: key }),
  });
  // The token counter depends on the backend, so the board and the stats bar
  // have to be redrawn, not just the panel.
  renderStats();
  renderBoard();
  await renderModelsPanel();
}

async function saveAcceptMode(key) {
  state.settings = await api('/api/settings', {
    method: 'PUT', body: JSON.stringify({ accept_mode: key }),
  });
  // The Accept button label follows the mode, so the board is redrawn too.
  renderBoard();
  await renderModelsPanel();
}

async function saveRoleModel(role, model) {
  const patch = {};
  patch[role] = model;
  const data = await api('/api/models', { method: 'PUT', body: JSON.stringify({ models: patch }) });
  state.models = data;
  await renderModelsPanel();
}

/* --- archive: the archive folders and the stacks/cards inside them --- */

/* Every level of the archive browser is a drop-down: a header button and a body
 * that folds out under it. The body is animated in place — height 0 -> its
 * natural height on the next frame, and back — instead of the whole panel being
 * rebuilt, so the fold is smooth and the content below slides instead of
 * jumping. The duration lives in style.css (--archive-drop-ms); keep the two in
 * sync. */
const ARCHIVE_DROP_MS = 180;

/* The chevron rotates (a CSS transition) instead of swapping ▾/▸ glyphs. */
function archiveChevron(open) {
  return el('span', 'archive-chevron' + (open ? ' archive-chevron--open' : ''), '▸');
}

function setArchiveChevron(head, open) {
  const chevron = head.querySelector('.archive-chevron');
  if (chevron) chevron.classList.toggle('archive-chevron--open', open);
}

/* The level's body, if it is currently mounted. */
function archiveBody(box, cls) {
  for (const child of box.children) if (child.classList.contains(cls)) return child;
  return null;
}

/* Run `done` when the body's fold transition is over. transitionend bubbles, so
 * a nested level folding at the same time must not end this one early; the
 * timer is the safety net for a transition that never fires (reduced motion,
 * a hidden panel). */
function afterFold(body, done) {
  let finished = false;
  const finish = (e) => {
    if (e && e.target !== body) return;
    if (finished) return;
    finished = true;
    body.removeEventListener('transitionend', finish);
    done();
  };
  body.addEventListener('transitionend', finish);
  setTimeout(finish, ARCHIVE_DROP_MS + 80);
}

/* Fold a body out: it starts at height 0 (padding included, so the fold starts
 * from nothing) and grows to its natural height on the next frame, which the
 * CSS transition animates. The inline height is dropped once the transition is
 * over so the body keeps growing with its content. */
function foldOut(body) {
  const natural = body.scrollHeight;
  body.classList.add('archive-drop--open');
  body.style.height = '0px';
  body.style.paddingTop = '0px';
  body.style.paddingBottom = '0px';
  // Force a style flush: without it the browser coalesces the pinned 0px and
  // the target height into one style change and the body pops open instead of
  // transitioning. The flush gives the transition its start value.
  void body.offsetHeight;
  requestAnimationFrame(() => {
    body.style.height = natural + 'px';
    body.style.paddingTop = '';
    body.style.paddingBottom = '';
  });
  afterFold(body, () => {
    if (!body.classList.contains('archive-drop--open')) return;  // folded away meanwhile
    body.classList.remove('archive-drop--open');
    body.style.height = '';
  });
}

/* Fold a body away: the state is already closed, so its content leaves at once;
 * the space it took collapses smoothly and the body is dropped when the
 * transition is over. */
function foldAway(body) {
  const natural = body.scrollHeight;
  body.classList.remove('archive-drop--open');
  body.classList.add('archive-drop--closing');
  body.style.height = natural + 'px';
  body.textContent = '';
  // Same flush as foldOut: the pinned natural height must be the transition's
  // start value, or the body pops shut.
  void body.offsetHeight;
  requestAnimationFrame(() => {
    body.style.height = '0px';
    body.style.paddingTop = '0px';
    body.style.paddingBottom = '0px';
  });
  afterFold(body, () => body.remove());
}

/* Open or fold one level in place. The panel is not rebuilt, so the fold can
 * animate and the rest of the tree keeps its nodes (and its scroll position). */
function toggleArchiveLevel(box, head, cls, willOpen, build) {
  const body = archiveBody(box, cls);
  if (!willOpen) {
    setArchiveChevron(head, false);
    if (body) foldAway(body);
    return;
  }
  if (body) body.remove();  // a body still folding away after a fast double click
  const fresh = build();
  box.appendChild(fresh);
  setArchiveChevron(head, true);
  foldOut(fresh);
}

async function renderArchivePanel() {
  const archives = await api('/api/archives');
  state.archives = Array.isArray(archives) ? archives : [];
  sidebarBody.textContent = '';
  if (!state.archives.length) {
    sidebarBody.appendChild(el('p', 'sidebar-empty',
      'No archives yet. Archive a card, a whole stack or a project from the board — '
      + 'each archive is a folder under archives/ holding its cards.'));
    return;
  }
  // Three levels: project header -> archive folder -> the stacks/cards inside.
  // Archives with no project sit under a "No project" header so nothing hides.
  const byProject = new Map();
  for (const a of state.archives) {
    const key = a.project || '';
    if (!byProject.has(key)) byProject.set(key, []);
    byProject.get(key).push(a);
  }
  const names = [...byProject.keys()].sort((x, y) => {
    if (x === '') return 1;            // "No project" last
    if (y === '') return -1;
    return x.localeCompare(y);
  });
  for (const name of names) {
    sidebarBody.appendChild(buildArchiveProject(name, byProject.get(name)));
  }
}

/* A project header: the top level of the archive browser. */
function buildArchiveProject(name, archives) {
  const box = el('div', 'archive-project');
  const open = state.sidebarProjectOpen.has(name);
  const head = el('button', 'archive-project-head');
  head.type = 'button';
  head.title = name
    ? 'Archives filed under the ' + name + ' project'
    : 'Archives not filed under a project — set one from the archive modal';
  head.appendChild(archiveChevron(open));
  head.appendChild(el('span', 'archive-project-name', name || 'No project'));
  head.appendChild(el('span', 'archive-item-id', String(archives.length)));
  head.addEventListener('click', () => {
    const willOpen = !state.sidebarProjectOpen.has(name);
    if (willOpen) state.sidebarProjectOpen.add(name);
    else state.sidebarProjectOpen.delete(name);
    toggleArchiveLevel(box, head, 'archive-project-body', willOpen,
      () => buildArchiveProjectBody(archives));
  });
  box.appendChild(head);
  if (open) box.appendChild(buildArchiveProjectBody(archives));
  return box;
}

function buildArchiveProjectBody(archives) {
  const body = el('div', 'archive-project-body');
  for (const a of archives) body.appendChild(buildArchiveFolder(a));
  return body;
}

function buildArchiveFolder(a) {
  const box = el('div', 'archive-folder');
  const open = state.sidebarArchiveOpen.has(a.id);
  const head = el('button', 'archive-folder-head');
  head.type = 'button';
  head.title = a.path || a.name;
  head.appendChild(archiveChevron(open));
  head.appendChild(el('span', 'archive-name', a.name));
  head.appendChild(el('span', 'archive-type', a.type || 'cards'));
  head.appendChild(el('span', 'archive-item-id', String(a.item_count || 0)));
  head.addEventListener('click', async () => {
    if (state.sidebarArchiveOpen.has(a.id)) {
      state.sidebarArchiveOpen.delete(a.id);
      toggleArchiveLevel(box, head, 'archive-folder-body', false, null);
      return;
    }
    // The list endpoint carries counts, not the cards: fetch them once and
    // keep them in state (a re-render rebuilds these objects from scratch).
    if (!state.archiveProjects.has(a.id)) {
      try {
        const full = await api('/api/archives/' + encodeURIComponent(a.id));
        state.archiveProjects.set(a.id, full.projects || []);
      } catch (err) { showError(err); }
    }
    state.sidebarArchiveOpen.add(a.id);
    toggleArchiveLevel(box, head, 'archive-folder-body', true,
      () => buildArchiveFolderBody(a));
  });
  box.appendChild(head);
  if (open) box.appendChild(buildArchiveFolderBody(a));
  return box;
}

function buildArchiveFolderBody(a) {
  const body = el('div', 'archive-folder-body');
  // The archive's cards, grouped by their own project. An archive that holds
  // one project (the normal case) shows its cards directly — a header there
  // would just repeat the project header the archive already sits under.
  const groups = state.archiveProjects.get(a.id) || [];
  if (groups.length === 1) {
    for (const n of (groups[0].cards || [])) body.appendChild(archiveRow(n.title || n.id, n.id));
    if (!(groups[0].cards || []).length) {
      body.appendChild(el('p', 'sidebar-empty', 'This folder is empty.'));
    }
  } else {
    for (const g of groups) body.appendChild(buildArchiveProjectGroup(a, g));
    if (!groups.length) {
      body.appendChild(el('p', 'sidebar-empty', 'This folder is empty.'));
    }
  }
  return body;
}

/* A project header inside an archive folder: the tree's third level. It is a
 * dropdown of its own — open by default (expanding an archive still shows its
 * cards), click to fold the project's cards away. */
function buildArchiveProjectGroup(a, g) {
  const key = a.id + '::' + (g.name || '');
  // Open by default: expanding an archive still shows its cards, one level
  // deeper. The set holds the headers the user folded away.
  const open = !state.sidebarArchiveProjects.has(key);
  const box = el('div', 'archive-project');
  const head = el('button', 'archive-project-head');
  head.type = 'button';
  head.title = g.name
    ? 'Project “' + g.name + '” — click to fold its cards'
    : 'Cards with no project — click to fold them';
  head.appendChild(archiveChevron(open));
  head.appendChild(el('span', 'archive-project-name', g.name || 'No project'));
  head.appendChild(el('span', 'archive-item-id', String(g.count || 0)));
  head.addEventListener('click', () => {
    const folded = state.sidebarArchiveProjects.has(key);
    if (folded) state.sidebarArchiveProjects.delete(key);
    else state.sidebarArchiveProjects.add(key);
    toggleArchiveLevel(box, head, 'archive-project-body', folded,
      () => buildArchiveProjectGroupBody(g));
  });
  box.appendChild(head);
  if (open) box.appendChild(buildArchiveProjectGroupBody(g));
  return box;
}

function buildArchiveProjectGroupBody(g) {
  const list = el('div', 'archive-project-body');
  for (const n of (g.cards || [])) list.appendChild(archiveRow(n.title || n.id, n.id));
  if (!(g.cards || []).length) {
    list.appendChild(el('p', 'sidebar-empty', 'No cards in this project.'));
  }
  return list;
}

function archiveRow(title, id) {
  const row = el('div', 'archive-item');
  if (id) row.dataset.cardId = id;  // a search result highlights the row by id
  row.appendChild(el('span', 'archive-item-title', title));
  if (id) row.appendChild(el('span', 'archive-item-id', id));
  return row;
}

/* --- split: does decomposing a card actually pay? --- */

/* The one question that decides whether an orchestrator earns its place: do
 * cards split into sub-cards cost less per ACCEPTED outcome than unsplit ones?
 * A split card's cost is its whole subtree, because that is what the split
 * actually spent. */
async function renderSplitPanel() {
  const report = await api('/api/split-report');
  sidebarBody.textContent = '';

  const table = el('div', 'split-table');
  const head = el('div', 'split-row split-row--head');
  head.appendChild(el('span', '', ''));
  head.appendChild(el('span', '', 'cards'));
  head.appendChild(el('span', '', 'accepted'));
  head.appendChild(el('span', '', 'tokens/accepted'));
  table.appendChild(head);
  for (const [key, label] of [['split', 'Split'], ['unsplit', 'Unsplit']]) {
    const b = report[key] || {};
    const row = el('div', 'split-row');
    row.appendChild(el('span', 'split-label', label));
    row.appendChild(el('span', '', String(b.cards || 0)));
    row.appendChild(el('span', '', String(b.accepted || 0)));
    row.appendChild(el('span', '', b.tokens_per_accepted ? fmtTokens(b.tokens_per_accepted) : '—'));
    table.appendChild(row);
  }
  sidebarBody.appendChild(table);

  const s = report.split || {};
  const u = report.unsplit || {};
  let verdict;
  if (!s.cards) {
    verdict = 'No split cards yet. When a worker breaks its spec into sub-cards, '
      + 'they show up here as children and this table fills in.';
  } else if (s.tokens_per_accepted && u.tokens_per_accepted) {
    const ratio = s.tokens_per_accepted / u.tokens_per_accepted;
    verdict = ratio < 1
      ? 'Splitting is paying off: ' + Math.round((1 - ratio) * 100) + '% fewer tokens per accepted outcome.'
      : 'Splitting is costing ' + Math.round((ratio - 1) * 100) + '% MORE tokens per accepted outcome — '
        + 'the decomposition is not buying context isolation.';
  } else {
    verdict = 'Not enough accepted outcomes on both sides to compare yet.';
  }
  sidebarBody.appendChild(el('p', 'sidebar-empty', verdict));

  const trees = report.trees || [];
  if (trees.length) {
    sidebarBody.appendChild(el('div', 'archive-group-title', 'Split cards'));
    for (const t of trees) {
      const row = el('button', 'archive-item split-tree');
      row.type = 'button';
      row.title = 'Jump to “' + t.title + '”';
      row.appendChild(el('span', 'archive-item-title', t.title));
      row.appendChild(el('span', 'archive-item-id', t.children + '⤷'));
      row.appendChild(el('span', 'archive-item-id', fmtTokens(t.tokens)));
      row.addEventListener('click', () => jumpToCard(t.id));
      sidebarBody.appendChild(row);
    }
  }
}

/* Live elapsed time on running cards (local only, no network). */
setInterval(() => {
  const now = Date.now();
  document.querySelectorAll('[data-elapsed-from]').forEach((node) => {
    const from = parseTs(node.dataset.elapsedFrom);
    if (from !== null) node.textContent = fmtDuration((now - from) / 1000);
  });
}, 1000);

/* ---------------- Desktop notifications ---------------- */

/* A card finishing is silent: the board just changes colour, so if you are in
 * another window you only find out by looking. Every refresh therefore diffs
 * the card statuses against the previous one and announces the cards that just
 * ended.
 *
 * The browser Notification API is the primary channel; when the user denied
 * permission we fall back to a short synthesised WebAudio beep (no audio file).
 * While the tab is hidden the title carries an unread count, so a glance at the
 * tab strip is enough. */

const NOTIFY_KEY = 'vibe-harness:notifications';
const TITLE_BASE = document.title || 'vibe-harness';

/* Statuses that mean "a session is in flight" and statuses that mean "it just
 * ended". Only a move from the first set to the second one is announced. */
const LIVE_STATUSES = new Set(['running', 'reviewing']);
const FINISHED_STATUSES = new Set(['done', 'failed', 'stopped']);

function loadNotificationsEnabled() {
  try { return localStorage.getItem(NOTIFY_KEY) !== '0'; } catch (_) { return true; }
}
function saveNotificationsEnabled(on) {
  try { localStorage.setItem(NOTIFY_KEY, on ? '1' : '0'); } catch (_) { /* private mode */ }
}

function notificationPermission() {
  try {
    if (typeof Notification === 'undefined') return 'unsupported';
    return Notification.permission;
  } catch (_) { return 'unsupported'; }
}

/* Ask on the first user gesture — never on load: a prompt nobody asked for is
 * noise, and browsers block it outside a gesture anyway. */
let permissionAsked = false;
function askNotificationPermission() {
  if (permissionAsked) return;
  permissionAsked = true;
  try {
    if (typeof Notification === 'undefined' || Notification.permission !== 'default') return;
    const asked = Notification.requestPermission();
    if (asked && typeof asked.catch === 'function') asked.catch(() => {});
  } catch (_) { /* the callback form of older browsers needs no handling */ }
}
document.addEventListener('click', askNotificationPermission, { once: true });

/* No audio asset: a short beep synthesised with WebAudio. One context is kept
 * for the tab's lifetime — browsers cap how many a page may create. */
let audioCtx = null;
function beep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audioCtx) audioCtx = new Ctx();
    const ctx = audioCtx;
    if (ctx.state === 'suspended' && typeof ctx.resume === 'function') ctx.resume();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = 880;
    gain.gain.value = 0.08;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.18);
  } catch (_) { /* audio blocked — nothing left to fall back to */ }
}

/* One notification per finished card, naming the card and its outcome. */
function notifyCard(card) {
  const outcome = card.status;
  const name = card.title || card.id;
  if (notificationPermission() === 'granted') {
    try {
      const note = new Notification(name, {
        body: 'Card ' + outcome + ' — click to open the board.',
        tag: 'vibe-harness:' + card.id,
      });
      note.onclick = () => { try { window.focus(); note.close(); } catch (_) { /* gone */ } };
      return;
    } catch (_) { /* fall through to the beep */ }
  }
  beep();
}

/* The card whose panel is open is the one the user is already looking at. */
function cardPanelOpen(cardId) {
  return state.editorCardId === cardId || state.logCardId === cardId;
}

/* Diff the new statuses against the previous refresh and return the cards that
 * just finished. The first refresh has no previous map, so it only records. */
function diffCardStatuses(cards) {
  const prev = state.prevStatuses;
  const next = new Map();
  const finished = [];
  for (const card of cards) {
    next.set(card.id, card.status);
    if (!prev) continue;                       // first load: nothing to compare
    const before = prev.get(card.id);
    if (before === undefined) continue;        // a card we have never seen
    if (LIVE_STATUSES.has(before) && FINISHED_STATUSES.has(card.status)) finished.push(card);
  }
  state.prevStatuses = next;
  return finished;
}

/* "(N) vibe-harness" while notifications are unread and the tab is hidden. */
function updateTitleBadge() {
  const unread = state.unreadNotifications;
  document.title = (unread > 0 && document.hidden) ? '(' + unread + ') vibe-harness' : TITLE_BASE;
}

function announceFinished(cards) {
  if (!state.notificationsEnabled) return;
  for (const card of cards) {
    if (cardPanelOpen(card.id)) continue;
    notifyCard(card);
    if (document.hidden) state.unreadNotifications += 1;
  }
  updateTitleBadge();
}

/* Coming back to the tab means the user has seen the board: drop the badge. */
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) state.unreadNotifications = 0;
  updateTitleBadge();
});

function setNotificationsEnabled(on) {
  state.notificationsEnabled = !!on;
  saveNotificationsEnabled(state.notificationsEnabled);
  // The toggle click is a user gesture, so it is a fine moment to ask.
  if (state.notificationsEnabled) askNotificationPermission();
  else state.unreadNotifications = 0;
  updateTitleBadge();
}

/* ---------------- Heartbeat: is this board still telling the truth? ---------------- */

/* A board is only as honest as its last successful poll. Browsers throttle
 * timers in background tabs (sometimes to once a minute), and the poll used to
 * stop entirely when nothing was live — so a tab could sit for minutes showing
 * cards that had already finished, with nothing on screen saying so. The
 * heartbeat shows the age of the data and turns amber, then red, when the board
 * can no longer be trusted. Clicking it refreshes immediately. */

const HEARTBEAT_LIVE_MS = 10000;    // under this the board is current
const HEARTBEAT_STALE_MS = 60000;   // over this it is definitely wrong

function heartbeatState() {
  if (state.pollError) return 'offline';
  if (state.lastPollAt === null) return 'waiting';
  const age = Date.now() - state.lastPollAt;
  if (age < HEARTBEAT_LIVE_MS) return 'live';
  if (age < HEARTBEAT_STALE_MS) return 'stale';
  return 'cold';
}

function formatAge(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm';
  return Math.floor(m / 60) + 'h';
}

function renderHeartbeat() {
  const node = $('heartbeat');
  if (!node) return;
  const kind = heartbeatState();
  const age = state.lastPollAt === null ? null : Date.now() - state.lastPollAt;
  node.dataset.state = kind;
  const label = node.querySelector('.heartbeat-label');
  if (label) {
    label.textContent = kind === 'offline' ? 'offline'
      : kind === 'waiting' ? 'connecting…'
      : kind === 'live' ? 'live'
      : (kind === 'stale' ? 'stale · ' : 'out of date · ') + formatAge(age);
  }
  node.title = state.pollError
    ? 'The last refresh failed: ' + state.pollError + ' — click to retry'
    : age === null
      ? 'Waiting for the first refresh'
      : 'Last update ' + formatAge(age) + ' ago — click to refresh now';
}

$('heartbeat').addEventListener('click', () => { refresh(); });

/* The label ages even when no poll is due, so a throttled background tab shows
 * its staleness the moment it is looked at again. */
setInterval(renderHeartbeat, 1000);

/* Coming back to the tab: refresh at once instead of waiting for the next tick
 * (a background tab's timers may have been throttled for minutes). */
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});

/* ---------------- Polling ---------------- */

function anyLive() {
  return state.cards.some((n) => n.status === 'running' || n.status === 'reviewing' || isResolving(n) || isQueued(n));
}

/* The board used to stop polling entirely when nothing was live, so an idle tab
 * never noticed a card finishing, an accept, or a change made from another
 * window — it just kept showing whatever it had. Poll slowly when idle instead
 * of not at all; the heartbeat below makes the age of the data visible. */
const LIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 10000;

function updatePolling() {
  const want = anyLive() ? LIVE_POLL_MS : IDLE_POLL_MS;
  if (state.pollTimer && state.pollIntervalMs === want) return;
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollIntervalMs = want;
  state.pollTimer = setInterval(refresh, want);
}

async function refresh() {
  try {
    const statsUrl = state.selectedProject === ''
      ? '/api/stats'
      : '/api/stats?project=' + encodeURIComponent(state.selectedProject);
    const results = await Promise.all([api('/api/cards'), api(statsUrl), api('/api/stacks')]);
    state.cards = results[0];
    state.stats = results[1];
    state.stacks = Array.isArray(results[2]) ? results[2] : [];
    state.lastPollAt = Date.now();
    state.pollError = null;
    announceFinished(diffCardStatuses(state.cards));
    renderStats();
    renderBoard();
    renderHeartbeat();
    updatePolling();
  } catch (err) {
    state.pollError = err && err.message ? err.message : String(err);
    renderHeartbeat();
    showError(err);
  }
}

/* ---------------- Templates ---------------- */

async function loadTemplates() {
  if (!state.templatesLoaded) {
    try {
      state.templates = await api('/api/templates');
      state.templatesLoaded = true;
    } catch (err) {
      showError(err);
      state.templates = [];
    }
  }
  templateSelect.textContent = '';
  const blank = el('option', '', '— blank card —');
  blank.value = '';
  templateSelect.appendChild(blank);
  for (const t of state.templates) {
    const opt = el('option', '', t.name);
    opt.value = t.name;
    templateSelect.appendChild(opt);
  }
  templateSelect.value = '';
}

templateSelect.addEventListener('change', () => {
  const t = state.templates.find((x) => x.name === templateSelect.value);
  // Blank ("— blank card —", value '') clears the body; otherwise load the template.
  cardBody.value = t ? t.body : '';
});

/* ---------------- Editor modal ---------------- */

/* The merge commit an accepted card's accept recorded — the one commit a
 * revert undoes. Cards accepted before merge commits were recorded have none
 * (the API refuses to revert them with a 409). */
function cardMergeCommit(card) {
  const s = (card && card.session) || {};
  return (s.git && s.git.merge_commit) || s.merge_commit || '';
}

/* The card's "runs after" links, each with a remove (×) button. A card with a
 * live session cannot change its dependencies (the API returns 409), so the
 * buttons are hidden while it runs. */
function renderDependsList(card, live) {
  dependsList.textContent = '';
  const deps = card.depends_on || [];
  if (!deps.length) {
    dependsList.appendChild(el('span', 'depends-empty',
      'No dependencies — drag this draft card onto another card to link them.'));
    return;
  }
  for (const dep of deps) {
    const row = el('span', 'depends-item');
    row.appendChild(el('span', 'depends-name', '⛓ runs after ' + depName(dep)));
    if (!live) {
      const rm = el('button', 'icon-btn depends-remove', '×');
      rm.type = 'button';
      rm.title = 'Remove this dependency';
      rm.addEventListener('click', (e) => {
        e.stopPropagation();
        action(async () => {
          await api('/api/cards/' + encodeURIComponent(card.id) + '/depends/' + encodeURIComponent(dep),
                    { method: 'DELETE' });
          await openEditor(card.id);  // re-render the list
        });
      });
      row.appendChild(rm);
    }
    dependsList.appendChild(row);
  }
}

async function openEditor(cardId) {
  state.editorCardId = cardId || null;
  state.editorUpdatedAt = null;
  hideError();
  hideEditConflict();
  if (cardId) {
    const card = await api('/api/cards/' + encodeURIComponent(cardId));
    // The version this editor loaded: sent back on save so a save that lost a
    // race is refused (409) instead of silently overwriting the other edit.
    state.editorUpdatedAt = card.updated_at || null;
    editorHeading.textContent = 'Edit card';
    templateField.hidden = true;
    cardTitle.value = card.title || '';
    cardBody.value = card.body || '';
    cardAutoRun.checked = !!card.auto_run;
    cardAutoAccept.checked = !!card.auto_accept;
    ensureProjectOption(card.project);
    cardProject.value = card.project || '';
    cardWorkdir.value = card.workdir || '';
    const live = card.status === 'running' || card.status === 'reviewing';
    dependsField.hidden = false;
    renderDependsList(card, live);
    // A card with a live session cannot be edited or deleted (API returns 409).
    cardTitle.disabled = live;
    cardBody.disabled = live;
    cardAutoRun.disabled = live;
    cardAutoAccept.disabled = live;
    cardProject.disabled = live;
    cardWorkdir.disabled = live;
    btnSave.hidden = false;
    btnSave.disabled = live;
    btnDelete.hidden = live;
    btnRun.hidden = live;
    btnStop.hidden = !live;
    // Accept is offered once the reviewer has approved the card — or, when the
    // review/fix loop exhausted its rounds, for the user to accept manually.
    const review = (card.session && card.session.review) || {};
    const reviewed = review.approved === true;
    const exhausted = review.exhausted === true;
    btnAccept.hidden = !(card.status === 'done' && (reviewed || exhausted));
    acceptHint.hidden = !(card.status === 'done' && !reviewed && !exhausted);
    // Revert undoes the accept: offered on accepted cards, and the confirm
    // dialog names the merge commit it will revert.
    const mergeCommit = cardMergeCommit(card);
    btnRevert.hidden = card.status !== 'accepted';
    btnRevert.title = mergeCommit
      ? 'Undo this card: revert merge commit ' + mergeCommit.slice(0, 7) + ' and reopen it as a draft'
      : 'Undo this card: revert its merge commit and reopen it as a draft';
    // A reverted card says so — the session keeps the revert bookkeeping.
    const revertedAt = card.session && card.session.reverted_at;
    revertNote.hidden = !revertedAt;
    revertNote.textContent = revertedAt
      ? '↩ Reverted on ' + new Date(revertedAt).toLocaleString()
        + (card.session.revert_commit ? ' (revert commit ' + card.session.revert_commit.slice(0, 7) + ')' : '')
        + ' — the card is a draft again and its session history is kept.'
      : '';
    // In pr mode accepting opens a pull request instead of merging locally, so
    // the button says what it does.
    const prMode = !!(state.settings && state.settings.accept_mode === 'pr');
    btnAccept.textContent = prMode ? '↗ Open PR' : '✓ Accept';
    btnAccept.title = prMode
      ? 'Push the card branch and open a pull request with the review verdict as its body'
      : 'Merge the card branch into the base branch';
    acceptHint.textContent = prMode
      ? 'Open PR is available once the reviewer approves the card — or after the review rounds are exhausted, as a manual accept.'
      : 'Accept is available once the reviewer approves the card — or after the review rounds are exhausted, as a manual accept.';
    // An accepted card shipped as a PR links to it.
    const prUrl = (card.session && card.session.pr_url) || '';
    cardPrLink.hidden = !prUrl;
    if (prUrl) {
      cardPrLink.href = prUrl;
      cardPrLink.textContent = '↗ Pull request'
        + (card.session.pr_number ? ' #' + card.session.pr_number : '');
      cardPrLink.title = 'Opened on accept: ' + prUrl;
    }
    // Only accepted cards can be archived.
    btnArchive.hidden = card.status !== 'accepted';
    btnLog.hidden = !card.session;
    btnDiff.hidden = !card.session;
  } else {
    editorHeading.textContent = 'New card';
    templateField.hidden = false;
    cardTitle.value = '';
    cardBody.value = '';
    cardAutoRun.checked = false;
    cardAutoAccept.checked = loadAutoAcceptDefault();
    // Automatically use the last used project (fallback: the top-bar selection).
    cardProject.value = defaultProjectForNewCard();
    cardWorkdir.value = '';
    dependsField.hidden = true;   // dependencies are linked after the card exists
    cardTitle.disabled = false;
    cardBody.disabled = false;
    cardAutoRun.disabled = false;
    cardAutoAccept.disabled = false;
    cardProject.disabled = false;
    cardWorkdir.disabled = false;
    btnSave.hidden = false;
    btnSave.disabled = false;
    btnDelete.hidden = true;
    btnRun.hidden = false;   // saves, then runs
    btnStop.hidden = true;
    btnAccept.hidden = true;
    btnAccept.textContent = '✓ Accept';
    acceptHint.hidden = true;
    btnRevert.hidden = true;
    revertNote.hidden = true;
    cardPrLink.hidden = true;
    btnArchive.hidden = true;
    btnLog.hidden = true;
    btnDiff.hidden = true;
    await loadTemplates();
    cardTitle.focus();
  }
  editorOverlay.hidden = false;
}

function closeEditor() {
  editorOverlay.hidden = true;
  state.editorCardId = null;
}

/* A save that lost a race: the card changed since this editor loaded it. The
 * user's text stays in the fields; these two buttons are the way out. */
function showEditConflict(detail) {
  editConflictText.textContent = '⚠ ' + (detail || 'This card changed since you opened it.');
  editConflict.hidden = false;
}

function hideEditConflict() {
  editConflict.hidden = true;
  editConflictText.textContent = '';
}

/* Save the open card. A lost race (409) shows the conflict box and returns
 * null, so the caller leaves the editor open with the user's text intact. */
async function saveCardOrConflict(opts) {
  hideEditConflict();
  try {
    return await saveCard(opts);
  } catch (err) {
    if (err && err.status === 409) {
      showEditConflict(err.message);
      return null;
    }
    throw err;
  }
}

async function saveCard(opts) {
  const force = !!(opts && opts.force);
  const title = cardTitle.value.trim();
  const body = cardBody.value;
  const autoRun = cardAutoRun.checked;
  const autoAccept = cardAutoAccept.checked;
  if (!title) throw new Error('Title is required.');
  const project = cardProject.value.trim() || null;
  const workdir = cardWorkdir.value.trim() || null;
  // The project this card is saved with (creating or editing) becomes the
  // last used one — the default for the next new card ('' = no project).
  saveLastProjectDefault(project || '');
  if (state.editorCardId) {
    const payload = { title: title, body: body, auto_run: autoRun, auto_accept: autoAccept, project: project, workdir: workdir };
    // Optimistic concurrency: name the version this editor loaded. "Overwrite
    // anyway" (force) leaves it out, which is the unconditional last-write-wins
    // save the MCP tools and scripts use.
    if (!force && state.editorUpdatedAt) payload.expected_updated_at = state.editorUpdatedAt;
    const saved = await api('/api/cards/' + encodeURIComponent(state.editorCardId), {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    state.editorUpdatedAt = saved.updated_at || null;
    return saved;
  }
  const payload = { title: title, body: body, auto_run: autoRun, auto_accept: autoAccept, project: project, workdir: workdir };
  if (templateSelect.value) payload.template = templateSelect.value;
  return api('/api/cards', { method: 'POST', body: JSON.stringify(payload) });
}

// Remember the auto-accept checkbox setting as soon as it changes.
cardAutoAccept.addEventListener('change', () => saveAutoAcceptDefault(cardAutoAccept.checked));

btnSave.addEventListener('click', () => action(async () => {
  const saved = await saveCardOrConflict();
  if (!saved) return;   // a lost race: the editor stays open with the conflict box
  closeEditor();
}));

// "Overwrite anyway": re-send without expected_updated_at — the user chose to
// replace the other person's edit.
btnOverwrite.addEventListener('click', () => action(async () => {
  await saveCard({ force: true });
  hideEditConflict();
  closeEditor();
}));

// "Reload theirs": discard the local text and load the version on the board.
btnReloadTheirs.addEventListener('click', () => action(async () => {
  if (state.editorCardId) await openEditor(state.editorCardId);
}));

// Archive the open card (only offered for accepted cards): pick an archive.
btnArchive.addEventListener('click', () => {
  if (!state.editorCardId) return;
  openArchives({ kind: 'card', id: state.editorCardId, label: cardTitle.value.trim() || state.editorCardId })
    .catch(showError);
});

btnRun.addEventListener('click', () => action(async () => {
  // Save first: edits are persisted, and a changed spec resets the card to draft.
  const saved = await saveCardOrConflict();
  if (!saved) return;   // a lost race: don't run the other person's version
  // With auto_run on, the save above already queued the run (the card is now
  // "running") — don't POST /run again, that would just 409 "already running"
  // while the run is in fact starting.
  if (saved.status !== 'running' && saved.status !== 'reviewing') {
    await api('/api/cards/' + encodeURIComponent(saved.id) + '/run', { method: 'POST' });
  }
  closeEditor();
}));

btnStop.addEventListener('click', () => action(async () => {
  await api('/api/cards/' + encodeURIComponent(state.editorCardId) + '/stop', { method: 'POST' });
  // Keep the editor open so the user can watch the status change.
}));

btnAccept.addEventListener('click', () => action(async () => {
  await api('/api/cards/' + encodeURIComponent(state.editorCardId) + '/accept', { method: 'POST' });
  closeEditor();
}));

// Revert an accepted card: undo its merge commit and reopen it as a draft.
// This rewrites the project's base branch, so it sits behind a confirm dialog
// that names the commit being reverted.
btnRevert.addEventListener('click', () => action(async () => {
  const id = state.editorCardId;
  if (!id) return;
  const card = await api('/api/cards/' + encodeURIComponent(id));
  const mergeCommit = cardMergeCommit(card);
  const what = mergeCommit ? 'merge commit ' + mergeCommit.slice(0, 7) : 'its merge commit';
  if (!window.confirm('Revert this card? This runs `git revert` on ' + what
      + ' in the project folder and reopens the card as a draft.')) return;
  await api('/api/cards/' + encodeURIComponent(id) + '/revert', { method: 'POST' });
  closeEditor();
}));

btnDelete.addEventListener('click', () => action(async () => {
  if (!window.confirm('Delete this card and its session files?')) return;
  await api('/api/cards/' + encodeURIComponent(state.editorCardId), { method: 'DELETE' });
  closeEditor();
}));

btnLog.addEventListener('click', () => {
  if (state.editorCardId) openLog(state.editorCardId);
});

btnDiff.addEventListener('click', () => {
  // The diff is a board-level popup, so the editor modal gets out of its way.
  if (!state.editorCardId) return;
  const cardId = state.editorCardId;
  closeEditor();
  openDiff(cardId).catch(showError);
});

$('editor-close').addEventListener('click', closeEditor);
editorOverlay.addEventListener('click', (e) => { if (e.target === editorOverlay) closeEditor(); });
$('new-card-btn').addEventListener('click', () => { openEditor(null).catch(showError); });

/* ---------------- Log viewer modal ---------------- */

function stopLogTimer() {
  if (state.logTimer) {
    clearInterval(state.logTimer);
    state.logTimer = null;
  }
}

async function fetchLog() {
  const id = state.logCardId;
  if (!id) return;
  try {
    const results = await Promise.all([
      api('/api/cards/' + encodeURIComponent(id) + '/log?tail=200'),
      api('/api/cards/' + encodeURIComponent(id)),
    ]);
    logContent.textContent = results[0].lines || '(log is empty)';
    logContent.scrollTop = logContent.scrollHeight;
    if (results[1].status === 'running') {
      if (!state.logTimer) state.logTimer = setInterval(fetchLog, 2000);
    } else {
      stopLogTimer();
    }
  } catch (err) {
    showError(err);
    stopLogTimer();
  }
}

function openLog(cardId) {
  state.logCardId = cardId;
  const card = state.cards.find((n) => n.id === cardId);
  logHeading.textContent = 'Log — ' + (card ? card.title : cardId);
  logOverlay.hidden = false;
  fetchLog();
}

function closeLog() {
  stopLogTimer();
  state.logCardId = null;
  logOverlay.hidden = true;
}

$('log-close').addEventListener('click', closeLog);
logOverlay.addEventListener('click', (e) => { if (e.target === logOverlay) closeLog(); });

/* ---------------- Floating panels (hold a key + click a card) ---------------- */

/* Both popups are the same idea: a small panel pinned next to the clicked
 * card, clamped to the viewport, never a full-screen overlay. The user can
 * move and resize either one, and the panel remembers where it ended up. */

const PANEL_GAP = 12;   // distance from the card and the viewport edge
const PANEL_LAYOUT_PREFIX = 'vibe-harness:panel:';   // + the panel's key
const PANEL_RESIZE_DEBOUNCE_MS = 250;   // a corner drag must not write per frame

/* The panel's current box. The rendered size is what counts — a native resize
 * grip writes the inline width/height too, so the inline style is only a
 * fallback for a hidden panel, which reports a 0x0 box. */
function panelLayout(panel) {
  const rect = panel.getBoundingClientRect ? panel.getBoundingClientRect() : null;
  const shown = rect && rect.width > 0 && rect.height > 0;
  return {
    width: shown ? rect.width : parseFloat(panel.style.width),
    height: shown ? rect.height : parseFloat(panel.style.height),
    left: shown ? rect.left : parseFloat(panel.style.left),
    top: shown ? rect.top : parseFloat(panel.style.top),
  };
}

/* The user's own size and place for one panel, or null when nothing usable is
 * stored. localStorage throws in private mode and the value is user-editable,
 * so every read is guarded and every field is checked. */
function loadPanelLayout(key) {
  let raw = null;
  try { raw = localStorage.getItem(PANEL_LAYOUT_PREFIX + key); } catch (_) { return null; }
  if (!raw) return null;
  let saved = null;
  try { saved = JSON.parse(raw); } catch (_) { return null; }
  if (!saved || typeof saved !== 'object') return null;
  const layout = {
    width: Number(saved.width),
    height: Number(saved.height),
    left: Number(saved.left),
    top: Number(saved.top),
  };
  const usable = layout.width > 0 && layout.height > 0
    && isFinite(layout.left) && isFinite(layout.top);
  return usable ? layout : null;
}

/* Remember one panel's layout. A native resize fires no event of its own, so
 * watch the box and write once the user stops dragging the corner. */
function savePanelLayout(key, layout) {
  if (!layout || !(layout.width > 0) || !(layout.height > 0)
    || !isFinite(layout.left) || !isFinite(layout.top)) return;
  try {
    localStorage.setItem(PANEL_LAYOUT_PREFIX + key, JSON.stringify({
      width: layout.width,
      height: layout.height,
      left: layout.left,
      top: layout.top,
    }));
  } catch (_) { /* private mode */ }
}

/* Watch a panel for resizes and hand back the "save it now" callback the drag
 * needs. The layout is captured when the resize happens, not when the debounce
 * fires: a panel closed in between must not save its hidden 0x0 box. */
function rememberPanelLayout(panel, key) {
  const save = () => savePanelLayout(key, panelLayout(panel));
  if (typeof ResizeObserver !== 'function') return save;
  let timer = null;
  let pending = null;
  const observer = new ResizeObserver(() => {
    // The harness just placed the panel: that box is not the user's choice.
    if (panel.dataset.positioning) { delete panel.dataset.positioning; return; }
    pending = panelLayout(panel);
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      savePanelLayout(key, pending);
    }, PANEL_RESIZE_DEBOUNCE_MS);
  });
  observer.observe(panel);
  return save;
}

/* Mark the box the harness is about to set as its own, so the observer above
 * can skip the notification it produces. Comparing the rendered box with the
 * inline style cannot do this: a native `resize: both` drag writes the inline
 * width/height too, so after any real resize the two match. The observer
 * consumes the mark; the timer clears it when the positioning did not change
 * the box (no notification then), so the next real resize is still saved. Not
 * requestAnimationFrame: frame callbacks run before the observer's, which would
 * drop the mark before the observer could see it. */
function markPanelPositioning(panel) {
  panel.dataset.positioning = '1';
  setTimeout(() => { delete panel.dataset.positioning; }, 0);
}

/* Place a panel next to the clicked card, clamped to the viewport: to its
 * right when there is room, else to its left. `saved` is the user's own layout
 * (see loadPanelLayout) — it wins over the anchor, still clamped. */
function positionFloatingPanel(panel, anchor, width, height, saved) {
  const vw = window.innerWidth || 1280;
  const vh = window.innerHeight || 800;
  const w = Math.min(saved ? saved.width : width, vw - 2 * PANEL_GAP);
  const h = Math.min(saved ? saved.height : height, vh - 2 * PANEL_GAP);
  let left = PANEL_GAP;
  let top = PANEL_GAP;
  if (saved) {
    left = saved.left;
    top = saved.top;
  } else if (anchor && typeof anchor.getBoundingClientRect === 'function') {
    const rect = anchor.getBoundingClientRect();
    left = rect.right + PANEL_GAP;
    if (left + w > vw - PANEL_GAP) left = rect.left - w - PANEL_GAP;
    top = rect.top;
  }
  left = Math.max(PANEL_GAP, Math.min(left, vw - w - PANEL_GAP));
  top = Math.max(PANEL_GAP, Math.min(top, vh - h - PANEL_GAP));
  panel.style.left = left + 'px';
  panel.style.top = top + 'px';
  panel.style.width = w + 'px';
  panel.style.height = h + 'px';
}

/* Drag a panel by its header: pointerdown on the header (but not on a button
 * in it) moves the panel with the pointer. `onEnd` runs when the drag ends, so
 * the caller can remember where the panel landed. */
function makePanelDraggable(panel, handle, onEnd) {
  let drag = null;
  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (e.target && e.target.closest && e.target.closest('button')) return;
    drag = {
      x: e.clientX,
      y: e.clientY,
      left: parseFloat(panel.style.left) || 0,
      top: parseFloat(panel.style.top) || 0,
    };
    if (handle.setPointerCapture) handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener('pointermove', (e) => {
    if (!drag) return;
    panel.style.left = (drag.left + e.clientX - drag.x) + 'px';
    panel.style.top = (drag.top + e.clientY - drag.y) + 'px';
  });
  const stop = () => {
    if (!drag) return;   // a pointerup without a drag is not a drag end
    drag = null;
    if (onEnd) onEnd();
  };
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);
}

/* ---------------- Live activity popup (hold C + click a card) ---------------- */

/* A small floating panel that shows what the model is doing RIGHT NOW. The
 * session log is the CLI's final output (empty until the run ends); the vibe
 * journal is appended while the session runs, so the popup polls
 * /api/cards/<id>/activity every second and appends what is new. It is NOT an
 * overlay: the board stays usable behind it. */

const ACTIVITY_WIDTH = 420;
const ACTIVITY_HEIGHT = 520;
const ACTIVITY_POLL_MS = 1000;
const ACTIVITY_STICK_PX = 24;   // "at the bottom" tolerance for auto-scroll

/* One event -> one row. Every piece of journal text is model-written, so it
 * goes in as a TEXT NODE (el() sets textContent) — never innerHTML. */
function activityEventRow(event) {
  const kind = event.kind || 'text';
  const row = el('div', 'activity-event activity-event--' + kind);
  if (kind === 'reasoning') {
    // Collapsed by default: the thinking is there when you want it, but it
    // does not push the actual work off the panel.
    const toggle = el('button', 'activity-thinking', 'thinking…');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    const text = el('div', 'activity-text', event.text || '');
    text.hidden = true;
    toggle.addEventListener('click', () => {
      const open = text.hidden;
      text.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.textContent = open ? 'thinking ▴' : 'thinking…';
    });
    row.appendChild(toggle);
    row.appendChild(text);
    return row;
  }
  if (kind === 'tool') {
    row.appendChild(el('span', 'activity-tool', event.tool || 'tool'));
    row.appendChild(el('code', 'activity-summary', event.summary || ''));
    return row;
  }
  if (kind === 'result') {
    row.classList.add(event.ok ? 'activity-event--ok' : 'activity-event--failed');
    row.appendChild(el('span', 'activity-ok', event.ok ? '✓' : '✗'));
    row.appendChild(el('code', 'activity-summary', event.summary || ''));
    return row;
  }
  if (kind === 'status') {
    row.appendChild(el('span', 'activity-status-text', event.text || ''));
    return row;
  }
  row.appendChild(el('div', 'activity-text', event.text || ''));
  return row;
}

/* Append events to the feed (newest at the bottom) and follow them unless the
 * user scrolled up to read something. */
function renderActivityEvents(events) {
  const rows = [];
  for (const event of events || []) {
    const row = activityEventRow(event);
    activityFeed.appendChild(row);
    rows.push(row);
  }
  if (rows.length && state.activityAutoScroll) scrollActivityToLatest();
  return rows;
}

function scrollActivityToLatest() {
  activityFeed.scrollTop = activityFeed.scrollHeight;
  state.activityAutoScroll = true;
  activityJump.hidden = true;
}

/* The feed's scroll listener: at the bottom it follows new events; scrolled up
 * it stops and offers "jump to latest". */
function onActivityScroll() {
  const atBottom = activityFeed.scrollTop + activityFeed.clientHeight
    >= activityFeed.scrollHeight - ACTIVITY_STICK_PX;
  state.activityAutoScroll = atBottom;
  activityJump.hidden = atBottom;
}

function activityCard() {
  return state.cards.find((n) => n.id === state.activityCardId) || null;
}

function activityIsLive() {
  const card = activityCard();
  return !!card && (card.status === 'running' || card.status === 'reviewing');
}

/* ---------------- Steering: send guidance to the running session ---------------- */

/* The conversation with the person watching: user -> worker and worker -> user.
 * Reviewer <-> worker traffic is internal and stays out of the popup. */
function isUserMessage(msg) {
  return !!msg && (msg.from === 'user' || msg.to === 'user');
}

/* One message -> one bubble: the user's guidance on the right, the worker's
 * reply on the left. Message text is user- and model-written, so it goes in as
 * a TEXT NODE (el() sets textContent) — never innerHTML. */
function activityMessageRow(msg) {
  const from = msg.from === 'user' ? 'user' : 'worker';
  const row = el('div', 'activity-message activity-message--' + from);
  row.appendChild(el('span', 'activity-message-from', from === 'user' ? 'you' : 'worker'));
  row.appendChild(el('div', 'activity-message-text', msg.text || ''));
  return row;
}

/* Append the messages the feed has not shown yet. The poll re-reads the whole
 * conversation, so the seq cursor is what keeps it from duplicating. */
function renderActivityMessages(messages) {
  const rows = [];
  for (const msg of messages || []) {
    if (!isUserMessage(msg)) continue;
    const seq = Number(msg.seq) || 0;
    if (seq <= state.activityMessageSeq) continue;
    state.activityMessageSeq = seq;
    const row = activityMessageRow(msg);
    activityFeed.appendChild(row);
    rows.push(row);
  }
  if (rows.length && state.activityAutoScroll) scrollActivityToLatest();
  return rows;
}

/* The footer only works while the card is live: guidance posted to a finished
 * card would sit in the mailbox with nobody to read it. */
function updateActivityCompose() {
  const live = activityIsLive();
  activityInput.disabled = !live;
  activitySend.disabled = !live;
  activityHint.hidden = live;
  activityInput.placeholder = live ? 'Send guidance…' : 'this card is not running';
}

/* Send: POST the guidance, then show the stored message immediately (the next
 * poll would show it a second later; the user should see it land now). */
async function sendActivityMessage() {
  const id = state.activityCardId;
  const text = (activityInput.value || '').trim();
  if (!id || !text || !activityIsLive()) return null;
  const msg = await api('/api/cards/' + encodeURIComponent(id) + '/messages', {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
  if (state.activityCardId !== id) return msg;   // closed or reopened meanwhile
  activityInput.value = '';
  renderActivityMessages([msg]);
  return msg;
}

/* Header: card title, status pill, live token count. */
function updateActivityHeader() {
  const card = activityCard();
  activityTitle.textContent = card ? (card.title || '(untitled)') : (state.activityCardId || '');
  const status = card ? card.status : '';
  activityStatus.textContent = status || '—';
  activityStatus.className = 'activity-status activity-status--' + (status || 'unknown');
  const session = (card && card.session) || {};
  const tokens = typeof session.live_tokens === 'number' ? session.live_tokens
    : (typeof session.card_tokens === 'number' ? session.card_tokens : null);
  activityTokens.textContent = tokens === null ? '' : fmtTokens(tokens) + ' tok';
  activityTokens.title = tokens === null ? ''
    : 'Tokens this card has spent so far: ' + fmtTokens(tokens)
      + tokenTypesSuffix(session.live_token_breakdown || session.card_token_breakdown);
  updateActivityCompose();
}

function stopActivityTimer() {
  if (state.activityTimer) {
    clearInterval(state.activityTimer);
    state.activityTimer = null;
  }
}

/* One poll: append what the journal gained since the cursor. A failed poll
 * never shows the error banner (it would flash once a second); a deleted card
 * stops the timer, a transient error keeps it so the next tick retries. */
async function pollActivity() {
  const id = state.activityCardId;
  if (!id) return;
  let gone = false;
  try {
    const [feed, convo] = await Promise.all([
      api('/api/cards/' + encodeURIComponent(id)
        + '/activity?cursor=' + state.activityCursor),
      api('/api/cards/' + encodeURIComponent(id) + '/messages'),
    ]);
    if (state.activityCardId !== id) return;   // closed or reopened meanwhile
    state.activityCursor = feed.cursor || 0;
    const events = feed.events || [];
    if (events.length) renderActivityEvents(events);
    renderActivityMessages(convo && convo.messages);
    updateActivityHeader();
  } catch (err) {
    gone = !!(err && err.status === 404);      // the card was deleted
  }
  if (state.activityCardId !== id) return;
  if (!gone && activityIsLive()) {
    if (!state.activityTimer) {
      state.activityTimer = setInterval(pollActivity, ACTIVITY_POLL_MS);
    }
  } else {
    stopActivityTimer();
  }
}

/* Open the popup for one card (only one at a time). A card that is not live
 * still shows its last session's activity — one fetch, no polling. */
function openActivity(cardId, anchor) {
  closeDiff();          // one popup at a time
  closeActivity();
  state.activityCardId = cardId;
  state.activityCursor = 0;
  state.activityMessageSeq = 0;
  state.activityAutoScroll = true;
  activityFeed.textContent = '';
  activityJump.hidden = true;
  activityPopup.hidden = false;
  markPanelPositioning(activityPopup);
  positionFloatingPanel(activityPopup, anchor, ACTIVITY_WIDTH, ACTIVITY_HEIGHT,
    loadPanelLayout('activity'));
  updateActivityHeader();
  return pollActivity();
}

function closeActivity() {
  stopActivityTimer();
  state.activityCardId = null;
  state.activityCursor = 0;
  state.activityMessageSeq = 0;
  activityPopup.hidden = true;
}

$('activity-close').addEventListener('click', closeActivity);
activityJump.addEventListener('click', scrollActivityToLatest);
activityFeed.addEventListener('scroll', onActivityScroll);
activityCompose.addEventListener('submit', (e) => {
  if (e && e.preventDefault) e.preventDefault();
  sendActivityMessage().catch(showError);
});
makePanelDraggable(activityPopup, activityHead, rememberPanelLayout(activityPopup, 'activity'));

/* ---------------- Diff popup: what the card actually changed ---------------- */

/* A diff needs room, so the panel is wider than the activity popup — still a
 * floating panel, not an overlay: the board stays usable behind it. */
const DIFF_WIDTH = 720;
const DIFF_HEIGHT = 640;

/* The three ranges the server knows (GET /api/cards/<id>/diff?range=…). */
const DIFF_RANGES = [
  { key: 'card', label: 'Card', hint: 'what accept merged' },
  { key: 'round', label: 'Round', hint: 'what the reviewer judged' },
  { key: 'worktree', label: 'Worktree', hint: 'uncommitted changes' },
];

/* Layout: side-by-side needs roughly twice the width, so it is the default
 * only when the panel is wide enough; unified wins on a narrow panel. */
const DIFF_MODES = [
  { key: 'unified', label: 'Unified' },
  { key: 'split', label: 'Side by side' },
];
const DIFF_SPLIT_MIN_WIDTH = 640;
const DIFF_COLLAPSE_AFTER = 6;
const DIFF_PALETTE_KEY = 'vibe.diff.palette';

/* The panel's real width: positionFloatingPanel clamps it to the viewport, so
 * a narrow window gets unified even though the CSS asks for 720px. */
function diffPanelWidth() {
  const w = parseFloat(diffPopup.style.width);
  return Number.isFinite(w) && w > 0 ? w : DIFF_WIDTH;
}

function effectiveDiffMode() {
  return state.diffMode
    || (diffPanelWidth() >= DIFF_SPLIT_MIN_WIDTH ? 'split' : 'unified');
}

/* The colourblind-safe palette is a per-browser choice, so it survives a
 * reload; a private-mode localStorage failure just falls back to the default. */
function loadDiffPalette() {
  try {
    return localStorage.getItem(DIFF_PALETTE_KEY) === 'colorblind' ? 'colorblind' : 'default';
  } catch (_) {
    return 'default';
  }
}

function saveDiffPalette(palette) {
  try { localStorage.setItem(DIFF_PALETTE_KEY, palette); } catch (_) { /* private mode */ }
}

state.diffPalette = loadDiffPalette();

/* One line of a unified diff -> its kind. The file headers (+++ / ---) are
 * checked BEFORE the +/- prefixes, or every header would look like a change. */
function classifyDiffLine(line) {
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
  if (line.startsWith('diff ') || line.startsWith('index ')
      || line.startsWith('new file') || line.startsWith('deleted file')
      || line.startsWith('similarity index') || line.startsWith('dissimilarity index')
      || line.startsWith('rename ') || line.startsWith('copy ')
      || line.startsWith('old mode') || line.startsWith('new mode')
      || line.startsWith('Binary files') || line.startsWith('\\')) {
    return 'meta';
  }
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'ctx';
}

/* The path a `diff --git a/x b/y` line introduces (null when unparseable). */
function diffLinePath(line) {
  const m = /^diff --git a\/(.*) b\/(.*)$/.exec(line);
  return m ? m[2] : null;
}

/* Inside a hunk the first character is the marker, so a deleted `--` line
 * (`---`) is a deletion, not a file header. The `---`/`+++` headers only
 * appear before the first hunk, where classifyDiffLine handles them. */
function classifyHunkLine(line) {
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('\\')) return 'meta';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'ctx';
}

/* A hunk header: `@@ -oldStart,oldCount +newStart,newCount @@`. */
const DIFF_HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/* One parsed row. `oldLine`/`newLine` are null on the side the row does not
 * exist; `text` has the +/-/space marker stripped (the gutter shows it). */
function diffRow(kind, oldLine, newLine, text) {
  return { kind, oldLine, newLine, text };
}

/* Parse a unified diff into files -> blocks -> rows. A block is either a
 * `meta` line (the file header, `index`, `---`, `+++`) or a `hunk` with its
 * header and rows. Line numbers restart from each hunk header, so they stay
 * right across hunks. */
function parseDiff(text) {
  const files = [];
  let file = null;
  let block = null;
  let oldNo = 0;
  let newNo = 0;
  const startFile = (path) => {
    file = { path, blocks: [], additions: 0, deletions: 0 };
    files.push(file);
    block = null;
  };
  const lines = text.split('\n');
  // A diff ends with a newline; the empty tail is not a context line (it would
  // add a phantom row and bump the last line number).
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  for (const raw of lines) {
    if (raw.startsWith('diff --git ')) {
      startFile(diffLinePath(raw));
      file.blocks.push({ type: 'meta', row: diffRow('meta', null, null, raw) });
      continue;
    }
    if (!file) startFile(null);
    const kind = block ? classifyHunkLine(raw) : classifyDiffLine(raw);
    if (kind === 'hunk') {
      const m = DIFF_HUNK_RE.exec(raw);
      oldNo = m ? Number(m[1]) : 0;
      newNo = m ? Number(m[2]) : 0;
      block = { type: 'hunk', header: diffRow('hunk', null, null, raw), rows: [] };
      file.blocks.push(block);
      continue;
    }
    if (kind === 'meta') {
      const row = diffRow('meta', null, null, raw);
      if (block) block.rows.push(row);
      else file.blocks.push({ type: 'meta', row });
      continue;
    }
    const row = diffRow(kind, null, null, raw.slice(1));
    if (kind === 'add') {
      row.newLine = newNo++;
      file.additions++;
    } else if (kind === 'del') {
      row.oldLine = oldNo++;
      file.deletions++;
    } else {
      row.oldLine = oldNo++;
      row.newLine = newNo++;
    }
    if (!block) {
      block = { type: 'hunk', header: null, rows: [] };
      file.blocks.push(block);
    }
    block.rows.push(row);
  }
  for (const f of files) {
    for (const b of f.blocks) {
      if (b.type === 'hunk') annotateWordDiff(b.rows);
    }
  }
  return files;
}

/* Tokens for the word-level pass: words, whitespace runs and single
 * punctuation characters. Splitting punctuation separately keeps `foo(a)` and
 * `foo(b)` from looking like one changed word. */
const DIFF_TOKEN_RE = /[A-Za-z0-9_]+|\s+|[^\sA-Za-z0-9_]/g;

/* The char-level refinement is capped: a minified line would otherwise make
 * the O(n*m) table enormous. Past the cap the changed block stays whole. */
const DIFF_CHAR_MAX = 400;

/* The table is O(n*m); a minified line would otherwise allocate a huge one.
 * Past the cap the whole block is marked changed instead. */
const DIFF_LCS_MAX_CELLS = 250000;

/* Longest common subsequence over two token arrays -> eq/del/add ops. */
function lcsOps(a, b) {
  const n = a.length;
  const m = b.length;
  if (!n || !m || n * m > DIFF_LCS_MAX_CELLS) {
    return [
      ...a.map((text) => ({ op: 'del', text })),
      ...b.map((text) => ({ op: 'add', text })),
    ];
  }
  const dp = [];
  for (let i = 0; i <= n; i++) dp.push(new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ op: 'eq', text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ op: 'del', text: a[i] });
      i++;
    } else {
      ops.push({ op: 'add', text: b[j] });
      j++;
    }
  }
  while (i < n) ops.push({ op: 'del', text: a[i++] });
  while (j < m) ops.push({ op: 'add', text: b[j++] });
  return ops;
}

/* Word-level diff, refined to characters inside each changed block: the line
 * is marked changed, and the changed span inside it is marked harder. */
function diffWords(a, b) {
  const ops = lcsOps(a.match(DIFF_TOKEN_RE) || [], b.match(DIFF_TOKEN_RE) || []);
  const out = [];
  let i = 0;
  while (i < ops.length) {
    if (ops[i].op === 'eq') {
      out.push(ops[i]);
      i++;
      continue;
    }
    let j = i;
    while (j < ops.length && ops[j].op !== 'eq') j++;
    const block = ops.slice(i, j);
    const del = block.filter((o) => o.op === 'del').map((o) => o.text).join('');
    const add = block.filter((o) => o.op === 'add').map((o) => o.text).join('');
    if (del && add && del.length <= DIFF_CHAR_MAX && add.length <= DIFF_CHAR_MAX) {
      out.push(...lcsOps(Array.from(del), Array.from(add)));
    } else {
      out.push(...block);
    }
    i = j;
  }
  return out;
}

/* The ops for one side of a pair: keep eq + this side's changes, drop the
 * other side. Adjacent segments with the same flag are merged so the DOM has
 * one span per changed run, not one per token. */
function segmentsFor(ops, side) {
  const out = [];
  for (const op of ops) {
    if (op.op !== 'eq' && op.op !== side) continue;
    const changed = op.op !== 'eq';
    const last = out[out.length - 1];
    if (last && last.changed === changed) last.text += op.text;
    else out.push({ text: op.text, changed });
  }
  return out;
}

/* Pair each run of `-` rows with the following `+` run and annotate both sides
 * with word-level segments. Extra rows on either side stay whole-line. */
function annotateWordDiff(rows) {
  let i = 0;
  while (i < rows.length) {
    if (rows[i].kind !== 'del') {
      i++;
      continue;
    }
    const dels = [];
    while (i < rows.length && rows[i].kind === 'del') dels.push(rows[i++]);
    const adds = [];
    while (i < rows.length && rows[i].kind === 'add') adds.push(rows[i++]);
    const pairs = Math.min(dels.length, adds.length);
    for (let k = 0; k < pairs; k++) {
      const ops = diffWords(dels[k].text, adds[k].text);
      dels[k].segments = segmentsFor(ops, 'del');
      adds[k].segments = segmentsFor(ops, 'add');
    }
  }
}

/* Side-by-side alignment: a context row is one pair; a run of `-` rows is
 * paired with the following `+` run row by row; the longer side keeps empty
 * cells. Meta/hunk rows span both columns. */
function alignHunkRows(rows) {
  const out = [];
  let i = 0;
  while (i < rows.length) {
    const row = rows[i];
    if (row.kind === 'ctx') {
      out.push({ left: row, right: row });
      i++;
      continue;
    }
    if (row.kind === 'del') {
      const dels = [];
      while (i < rows.length && rows[i].kind === 'del') dels.push(rows[i++]);
      const adds = [];
      while (i < rows.length && rows[i].kind === 'add') adds.push(rows[i++]);
      const n = Math.max(dels.length, adds.length);
      for (let k = 0; k < n; k++) out.push({ left: dels[k] || null, right: adds[k] || null });
      continue;
    }
    if (row.kind === 'add') {
      const adds = [];
      while (i < rows.length && rows[i].kind === 'add') adds.push(rows[i++]);
      for (const add of adds) out.push({ left: null, right: add });
      continue;
    }
    out.push({ left: row, right: null, full: true });
    i++;
  }
  return out;
}

/* A run of more than DIFF_COLLAPSE_AFTER unchanged items becomes one
 * "⋯ N unchanged lines" row; the items themselves are rendered hidden so a
 * click expands them without a re-render. */
function collapseRuns(items, isContext, makeItem, makeCollapsed) {
  const out = [];
  let i = 0;
  while (i < items.length) {
    if (!isContext(items[i])) {
      out.push(makeItem(items[i++]));
      continue;
    }
    let j = i;
    while (j < items.length && isContext(items[j])) j++;
    const run = items.slice(i, j);
    if (run.length > DIFF_COLLAPSE_AFTER) out.push(makeCollapsed(run));
    else for (const item of run) out.push(makeItem(item));
    i = j;
  }
  return out;
}

function collapseContext(rows) {
  return collapseRuns(rows,
    (row) => row.kind === 'ctx',
    (row) => ({ type: 'row', row }),
    (run) => ({ type: 'collapsed', rows: run }));
}

/* The same collapse over aligned pairs (both sides context). */
function collapsePairs(pairs) {
  return collapseRuns(pairs,
    (pair) => pair.left && pair.right && pair.left.kind === 'ctx' && pair.right.kind === 'ctx',
    (pair) => ({ type: 'pair', pair }),
    (run) => ({ type: 'collapsed', pairs: run }));
}

function renderDiffRanges() {
  diffRanges.textContent = '';
  for (const range of DIFF_RANGES) {
    const btn = el('button', 'diff-range' + (state.diffRange === range.key ? ' diff-range--on' : ''),
                   range.label);
    btn.type = 'button';
    btn.dataset.range = range.key;
    btn.title = range.hint;
    btn.setAttribute('aria-pressed', state.diffRange === range.key ? 'true' : 'false');
    btn.addEventListener('click', () => {
      if (state.diffRange === range.key) return;
      state.diffRange = range.key;
      renderDiffRanges();
      loadDiff().catch(showError);
    });
    diffRanges.appendChild(btn);
  }
}

/* The verdict that judged this range, pinned next to the diff: approved /
 * rejected, the reviewer's reason, the round, the model and the tokens. */
function renderDiffVerdict(review) {
  diffVerdict.textContent = '';
  if (!review) {
    diffVerdict.appendChild(el('span', 'diff-verdict-none', 'No review verdict yet'));
    return;
  }
  const approved = review.approved === true;
  diffVerdict.appendChild(el('span',
    'diff-badge ' + (approved ? 'diff-badge--ok' : 'diff-badge--bad'),
    approved ? '✓ approved' : '✗ rejected'));
  const meta = el('div', 'diff-verdict-meta');
  if (review.round !== undefined && review.round !== null) {
    meta.appendChild(el('span', 'diff-verdict-item', 'round ' + review.round));
  }
  if (review.model) meta.appendChild(el('span', 'diff-verdict-item', review.model));
  if (review.tokens !== undefined && review.tokens !== null) {
    meta.appendChild(el('span', 'diff-verdict-item', review.tokens + ' tokens'));
  }
  if (review.via) meta.appendChild(el('span', 'diff-verdict-item', 'via ' + review.via));
  diffVerdict.appendChild(meta);
  if (review.reason) {
    diffVerdict.appendChild(el('p', 'diff-verdict-reason', review.reason));
  }
}

/* The changed files with their +/- counts; clicking one scrolls to it. */
function renderDiffFiles(files) {
  diffFiles.textContent = '';
  if (!files || !files.length) {
    diffFiles.appendChild(el('p', 'diff-files-empty', 'No files'));
    return;
  }
  for (const file of files) {
    const row = el('button', 'diff-file');
    row.type = 'button';
    row.title = file.path + ' — click to scroll to it';
    row.appendChild(el('span', 'diff-file-path', file.path));
    row.appendChild(el('span', 'diff-file-add', '+' + file.additions));
    row.appendChild(el('span', 'diff-file-del', '−' + file.deletions));
    row.addEventListener('click', () => scrollToDiffFile(file.path));
    diffFiles.appendChild(row);
  }
}

function scrollToDiffFile(path) {
  const target = diffBody.querySelector('[data-path="' + CSS.escape(path) + '"]');
  if (target) target.scrollIntoView({ block: 'start' });
}

/* The layout toggle: unified on a narrow panel, side-by-side when there is
 * room. The choice is per-session; the palette below is the persisted one. */
function renderDiffViews() {
  diffViews.textContent = '';
  const mode = effectiveDiffMode();
  for (const view of DIFF_MODES) {
    const btn = el('button', 'diff-tool diff-view' + (mode === view.key ? ' diff-tool--on' : ''),
                   view.label);
    btn.type = 'button';
    btn.dataset.mode = view.key;
    btn.setAttribute('aria-pressed', mode === view.key ? 'true' : 'false');
    btn.addEventListener('click', () => {
      if (effectiveDiffMode() === view.key) return;
      state.diffMode = view.key;
      if (state.diffLast) renderDiff(state.diffLast);
      else renderDiffViews();
    });
    diffViews.appendChild(btn);
  }
}

/* The palette is a data attribute on the body; the CSS variables do the rest,
 * so toggling it never re-renders the diff. */
function applyDiffPalette() {
  diffBody.dataset.palette = state.diffPalette;
  const on = state.diffPalette === 'colorblind';
  diffPaletteBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  diffPaletteBtn.classList.toggle('diff-tool--on', on);
}

/* ---------------- Syntax highlighting ---------------- */

/* The diff is model-written text, so the tokenizer is hand-written and
 * dependency-free: it scans one line into {text, cls} runs and carries the
 * multi-line state (an open block comment, an open string) to the next line,
 * so a Python docstring or a JS block comment is coloured on every line it
 * spans. `cls` is '' for plain text. */

const HIGHLIGHT_EXT = {
  py: 'python', pyi: 'python',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'javascript', tsx: 'javascript',
  css: 'css', scss: 'css',
  html: 'html', htm: 'html', svg: 'html', xml: 'html',
  md: 'markdown', markdown: 'markdown',
  json: 'json',
};

/* The language of a diffed file, from its path; anything unknown is plain. */
function highlightLang(path) {
  const m = /\.([A-Za-z0-9]+)$/.exec(path || '');
  return (m && HIGHLIGHT_EXT[m[1].toLowerCase()]) || 'plain';
}

function highlightState() {
  return { inBlockComment: false, inString: null };
}

/* Adjacent runs of the same class are merged: fewer spans, same colours. */
function pushToken(tokens, text, cls) {
  if (!text) return;
  const last = tokens[tokens.length - 1];
  if (last && last.cls === cls) last.text += text;
  else tokens.push({ text, cls });
}

const PY_KEYWORDS = new Set(('False None True and as assert async await break class continue '
  + 'def del elif else except finally for from global if import in is lambda nonlocal not '
  + 'or pass raise return try while with yield').split(' '));

const JS_KEYWORDS = new Set(('await break case catch class const continue debugger default '
  + 'delete do else export extends finally for function if import in instanceof let new of '
  + 'return static super switch this throw try typeof var void while with yield async '
  + 'true false null undefined').split(' '));

/* The per-language rules the shared code scanner reads. `multiline` lists the
 * quotes that may stay open at the end of a line (a Python docstring, a JS
 * template literal). */
const CODE_RULES = {
  python: {
    keywords: PY_KEYWORDS,
    lineComment: '#',
    blockComment: null,
    quotes: ['"""', "'''", '"', "'"],
    multiline: new Set(['"""', "'''"]),
    fnCall: true,
    css: false,
    json: false,
  },
  javascript: {
    keywords: JS_KEYWORDS,
    lineComment: '//',
    blockComment: ['/*', '*/'],
    quotes: ['"', "'", '`'],
    multiline: new Set(['`']),
    fnCall: true,
    css: false,
    json: false,
  },
  css: {
    keywords: new Set(),
    lineComment: null,
    blockComment: ['/*', '*/'],
    quotes: ['"', "'"],
    multiline: new Set(),
    fnCall: false,
    css: true,
    json: false,
  },
  json: {
    keywords: new Set(['true', 'false', 'null']),
    lineComment: null,
    blockComment: null,
    quotes: ['"'],
    multiline: new Set(),
    fnCall: false,
    css: false,
    json: true,
  },
};

/* Where the string opened at `from` closes, or -1 when it runs past the line.
 * A backslash escapes the next character, so `'it\'s'` stays one string. */
function stringEnd(text, from, quote) {
  let i = from;
  while (i < text.length) {
    if (text[i] === '\\') { i += 2; continue; }
    if (text.startsWith(quote, i)) return i + quote.length;
    i++;
  }
  return -1;
}

/* The end of the whitespace run that starts at `i`. */
function spaceEnd(text, i) {
  let j = i + 1;
  while (j < text.length && /\s/.test(text[j])) j++;
  return j;
}

const NUMBER_RE = /^(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?\d+)?|\.\d[\d_]*(?:[eE][+-]?\d+)?)/;
const WORD_RE = /^[A-Za-z_$][\w$]*/;
// CSS/SCSS identifiers: a hyphenated property (`min-width`) or an SCSS
// variable (`$brand`). The first-character class must cover every character
// the identifier branch below accepts, or the scan would throw on the rest.
const CSS_WORD_RE = /^[$A-Za-z_][\w$-]*/;
const OP_CHARS = '+-*/%=<>!&|^~@?';

/* Python, JavaScript, CSS and JSON: one scanner, four rule sets. */
function scanCode(text, state, rules) {
  const tokens = [];
  const n = text.length;
  let i = 0;
  // A state carried over from another language cannot open a block comment
  // this one has no syntax for.
  if (state.inBlockComment && !rules.blockComment) state.inBlockComment = false;
  if (state.inBlockComment) {
    const close = text.indexOf(rules.blockComment[1]);
    if (close < 0) {
      pushToken(tokens, text, 'tok-comment');
      return tokens;
    }
    const end = close + rules.blockComment[1].length;
    pushToken(tokens, text.slice(0, end), 'tok-comment');
    state.inBlockComment = false;
    i = end;
  }
  if (state.inString) {
    const end = stringEnd(text, i, state.inString);
    if (end < 0) {
      pushToken(tokens, text.slice(i), 'tok-string');
      return tokens;
    }
    pushToken(tokens, text.slice(i, end), 'tok-string');
    state.inString = null;
    i = end;
  }
  // CSS: after a declaration's colon the identifiers are values, not selectors.
  let inValue = false;
  while (i < n) {
    const ch = text[i];
    if (/\s/.test(ch)) {
      const j = spaceEnd(text, i);
      pushToken(tokens, text.slice(i, j), '');
      i = j;
      continue;
    }
    if (rules.lineComment && text.startsWith(rules.lineComment, i)) {
      pushToken(tokens, text.slice(i), 'tok-comment');
      break;
    }
    if (rules.blockComment && text.startsWith(rules.blockComment[0], i)) {
      const close = text.indexOf(rules.blockComment[1], i + rules.blockComment[0].length);
      if (close < 0) {
        pushToken(tokens, text.slice(i), 'tok-comment');
        state.inBlockComment = true;
        break;
      }
      const end = close + rules.blockComment[1].length;
      pushToken(tokens, text.slice(i, end), 'tok-comment');
      i = end;
      continue;
    }
    const quote = rules.quotes.find((q) => text.startsWith(q, i));
    if (quote) {
      const end = stringEnd(text, i + quote.length, quote);
      if (end < 0) {
        pushToken(tokens, text.slice(i), 'tok-string');
        if (rules.multiline.has(quote)) state.inString = quote;
        break;
      }
      // A JSON key is the string before the colon.
      const isKey = rules.json && /^\s*:/.test(text.slice(end));
      pushToken(tokens, text.slice(i, end), isKey ? 'tok-attr' : 'tok-string');
      i = end;
      continue;
    }
    if (rules.css && ch === '#' && /^#[0-9a-fA-F]{3,8}\b/.test(text.slice(i))) {
      const m = /^#[0-9a-fA-F]{3,8}/.exec(text.slice(i));
      pushToken(tokens, m[0], 'tok-number');
      i += m[0].length;
      continue;
    }
    if (rules.css && ch === '@') {
      const m = /^@[\w-]+/.exec(text.slice(i));
      if (m) {
        pushToken(tokens, m[0], 'tok-keyword');
        i += m[0].length;
        continue;
      }
    }
    if (rules.css && ch === '!' && /^!important\b/.test(text.slice(i))) {
      pushToken(tokens, '!important', 'tok-keyword');
      i += '!important'.length;
      continue;
    }
    if (/[0-9]/.test(ch) || (ch === '.' && /[0-9]/.test(text[i + 1] || ''))) {
      const m = NUMBER_RE.exec(text.slice(i));
      pushToken(tokens, m[0], 'tok-number');
      i += m[0].length;
      continue;
    }
    if (/[A-Za-z_$]/.test(ch)) {
      const m = (rules.css ? CSS_WORD_RE : WORD_RE).exec(text.slice(i));
      // No match means the guard and the regex disagree: fall through to the
      // single-character handling instead of throwing on a null match.
      if (m) {
        const word = m[0];
        const after = text[i + word.length];
        let cls = '';
        if (rules.keywords.has(word)) cls = 'tok-keyword';
        else if (rules.fnCall && after === '(') cls = 'tok-fn';
        else if (rules.css && !inValue && after === ':') cls = 'tok-attr';
        else if (rules.css && !inValue) cls = 'tok-tag';
        pushToken(tokens, word, cls);
        i += word.length;
        continue;
      }
    }
    if (OP_CHARS.includes(ch)) {
      let j = i + 1;
      while (j < n && OP_CHARS.includes(text[j])) j++;
      pushToken(tokens, text.slice(i, j), 'tok-op');
      i = j;
      continue;
    }
    if ('()[]{},:.;'.includes(ch)) {
      pushToken(tokens, ch, 'tok-punct');
      if (rules.css) {
        if (ch === ':') inValue = true;
        else if (ch === ';' || ch === '{' || ch === '}') inValue = false;
      }
      i++;
      continue;
    }
    pushToken(tokens, ch, '');
    i++;
  }
  return tokens;
}

/* HTML: tags, attributes and quoted values; text between tags stays plain. */
function scanHtml(text, state) {
  const tokens = [];
  const n = text.length;
  let i = 0;
  if (state.inBlockComment) {
    const close = text.indexOf('-->');
    if (close < 0) {
      pushToken(tokens, text, 'tok-comment');
      return tokens;
    }
    pushToken(tokens, text.slice(0, close + 3), 'tok-comment');
    state.inBlockComment = false;
    i = close + 3;
  }
  while (i < n) {
    if (text.startsWith('<!--', i)) {
      const close = text.indexOf('-->', i + 4);
      if (close < 0) {
        pushToken(tokens, text.slice(i), 'tok-comment');
        state.inBlockComment = true;
        break;
      }
      pushToken(tokens, text.slice(i, close + 3), 'tok-comment');
      i = close + 3;
      continue;
    }
    if (text[i] === '<') {
      const tag = /^<\/?[A-Za-z][\w:-]*/.exec(text.slice(i));
      if (tag) {
        pushToken(tokens, text.slice(i, i + tag[0].length), 'tok-tag');
        i += tag[0].length;
        while (i < n && text[i] !== '>') {
          const ch = text[i];
          if (/\s/.test(ch)) {
            const j = spaceEnd(text, i);
            pushToken(tokens, text.slice(i, j), '');
            i = j;
          } else if (ch === '"' || ch === "'") {
            const end = stringEnd(text, i + 1, ch);
            const stop = end < 0 ? n : end;
            pushToken(tokens, text.slice(i, stop), 'tok-string');
            i = stop;
          } else if (ch === '=') {
            pushToken(tokens, ch, 'tok-op');
            i++;
          } else if (/[A-Za-z_:@-]/.test(ch)) {
            const m = /^[A-Za-z_:@-][\w:.-]*/.exec(text.slice(i));
            pushToken(tokens, m[0], 'tok-attr');
            i += m[0].length;
          } else {
            pushToken(tokens, ch, 'tok-punct');
            i++;
          }
        }
        if (i < n) {
          pushToken(tokens, '>', 'tok-tag');
          i++;
        }
        continue;
      }
      const doctype = /^<![^>]*>/.exec(text.slice(i));
      if (doctype) {
        pushToken(tokens, doctype[0], 'tok-tag');
        i += doctype[0].length;
        continue;
      }
      pushToken(tokens, '<', 'tok-punct');
      i++;
      continue;
    }
    const next = text.indexOf('<', i);
    const end = next < 0 ? n : next;
    pushToken(tokens, text.slice(i, end), '');
    i = end;
  }
  return tokens;
}

/* Markdown: headings, inline code, emphasis, links and list markers. */
function scanMarkdown(text) {
  const tokens = [];
  if (/^#{1,6}\s/.test(text)) {
    pushToken(tokens, text, 'tok-heading');
    return tokens;
  }
  if (/^\s*(```|~~~)/.test(text)) {
    pushToken(tokens, text, 'tok-comment');
    return tokens;
  }
  const n = text.length;
  let i = 0;
  while (i < n) {
    const ch = text[i];
    if (ch === '`') {
      const close = text.indexOf('`', i + 1);
      if (close > i) {
        pushToken(tokens, text.slice(i, close + 1), 'tok-string');
        i = close + 1;
        continue;
      }
    }
    if (ch === '[') {
      const close = text.indexOf(']', i + 1);
      if (close > i) {
        pushToken(tokens, '[', 'tok-punct');
        pushToken(tokens, text.slice(i + 1, close), 'tok-attr');
        pushToken(tokens, ']', 'tok-punct');
        i = close + 1;
        if (text[i] === '(') {
          const end = text.indexOf(')', i + 1);
          if (end > i) {
            pushToken(tokens, '(', 'tok-punct');
            pushToken(tokens, text.slice(i + 1, end), 'tok-string');
            pushToken(tokens, ')', 'tok-punct');
            i = end + 1;
          }
        }
        continue;
      }
    }
    if ((ch === '-' || ch === '+' || ch === '*') && /^\s*$/.test(text.slice(0, i))) {
      pushToken(tokens, ch, 'tok-punct');
      i++;
      continue;
    }
    if (ch === '>' && /^\s*$/.test(text.slice(0, i))) {
      pushToken(tokens, ch, 'tok-punct');
      i++;
      continue;
    }
    if (ch === '*' || ch === '_') {
      let j = i;
      while (j < n && text[j] === ch) j++;
      pushToken(tokens, text.slice(i, j), 'tok-keyword');
      i = j;
      continue;
    }
    let j = i + 1;
    while (j < n && !'`[*_>'.includes(text[j])) j++;
    pushToken(tokens, text.slice(i, j), '');
    i = j;
  }
  return tokens;
}

/* One line -> {tokens, state}. `state` is mutated and returned, so a caller
 * can thread one object through a file's rows. */
function highlightLine(text, lang, state) {
  const st = state || highlightState();
  if (st.inBlockComment === undefined) st.inBlockComment = false;
  if (st.inString === undefined) st.inString = null;
  const line = text === null || text === undefined ? '' : String(text);
  let tokens;
  if (lang === 'html') tokens = scanHtml(line, st);
  else if (lang === 'markdown') tokens = scanMarkdown(line);
  else if (CODE_RULES[lang]) tokens = scanCode(line, st, CODE_RULES[lang]);
  else tokens = [{ text: line, cls: '' }];
  return { tokens, state: st };
}

/* Highlight one row and advance the file's multi-line state. Meta and hunk
 * rows are diff metadata, not code, so they stay plain. */
function highlightRow(row, hl) {
  if (!hl || row.kind === 'meta' || row.kind === 'hunk') return null;
  const res = highlightLine(row.text, hl.lang, hl.state);
  hl.state = res.state;
  return res.tokens;
}

/* Split the highlighted tokens by the word-diff segments. Both cover the same
 * line, so walking them together keeps every character in exactly one run. */
function tokenRuns(tokens, segments) {
  if (!segments || !segments.length) {
    return tokens.map((t) => ({ text: t.text, cls: t.cls, changed: false }));
  }
  const out = [];
  let ti = 0;
  let si = 0;
  let toff = 0;
  let soff = 0;
  while (ti < tokens.length && si < segments.length) {
    const tok = tokens[ti];
    const seg = segments[si];
    const take = Math.min(tok.text.length - toff, seg.text.length - soff);
    if (take > 0) {
      out.push({ text: tok.text.slice(toff, toff + take), cls: tok.cls, changed: seg.changed });
      toff += take;
      soff += take;
    }
    if (toff >= tok.text.length) { ti++; toff = 0; }
    if (soff >= seg.text.length) { si++; soff = 0; }
  }
  return out;
}

function lineNumber(n) {
  return n === null || n === undefined ? '' : String(n);
}

function diffMarker(kind) {
  if (kind === 'add') return '+';
  if (kind === 'del') return '-';
  return ' ';
}

/* The code cell: the line is highlighted into token runs, then split by the
 * word-diff segments. The token colour wins for the text and the word tint
 * stays as the span's background, so both signals survive. Every run is a
 * TEXT NODE — the model-written text never goes through innerHTML. */
function appendDiffCode(container, row, tokens) {
  if (row.text === '') {
    container.appendChild(document.createTextNode(' '));
    return;
  }
  const runs = tokenRuns(tokens || [{ text: row.text, cls: '' }], row.segments);
  for (const run of runs) {
    if (!run.text) continue;
    if (run.changed) {
      const span = el('span', 'diff-word diff-word--' + row.kind);
      if (run.cls) span.appendChild(el('span', run.cls, run.text));
      else span.appendChild(document.createTextNode(run.text));
      container.appendChild(span);
    } else if (run.cls) {
      container.appendChild(el('span', run.cls, run.text));
    } else {
      container.appendChild(document.createTextNode(run.text));
    }
  }
}

/* A meta or hunk line spans the full width: no line numbers, no marker. */
function renderFullRow(row) {
  const div = el('div', 'diff-row diff-row--' + row.kind);
  div.appendChild(document.createTextNode(row.text === '' ? ' ' : row.text));
  return div;
}

/* A run of unchanged lines: the button stays, the rows are rendered hidden so
 * a click expands them without a re-render. */
function collapsedRow(count, group) {
  const btn = el('button', 'diff-collapsed');
  btn.type = 'button';
  btn.setAttribute('aria-expanded', 'false');
  btn.appendChild(document.createTextNode('⋯ ' + count + ' unchanged lines'));
  btn.addEventListener('click', () => {
    const open = group.hidden;
    group.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.classList.toggle('diff-collapsed--open', open);
  });
  return btn;
}

function renderUnifiedRow(row, tokens) {
  const div = el('div', 'diff-row diff-row--' + row.kind);
  const gutter = el('span', 'diff-gutter');
  gutter.appendChild(el('span', 'diff-ln diff-ln--old', lineNumber(row.oldLine)));
  gutter.appendChild(el('span', 'diff-ln diff-ln--new', lineNumber(row.newLine)));
  gutter.appendChild(el('span', 'diff-marker', diffMarker(row.kind)));
  div.appendChild(gutter);
  const code = el('span', 'diff-code');
  appendDiffCode(code, row, tokens);
  div.appendChild(code);
  return div;
}

/* The collapsed button and its hidden rows, shared by both layouts. */
function appendCollapsed(container, count, items, renderItem) {
  const group = el('div', 'diff-collapsed-rows');
  group.hidden = true;
  for (const item of items) group.appendChild(renderItem(item));
  container.appendChild(collapsedRow(count, group));
  container.appendChild(group);
}

/* `hl` is the file's highlighter: its language plus the multi-line state the
 * rows advance in order (hidden collapsed rows included, so the state stays
 * right when they are expanded). */
function renderUnifiedRows(container, rows, hl) {
  const renderRow = (row) => renderUnifiedRow(row, highlightRow(row, hl));
  for (const item of collapseContext(rows)) {
    if (item.type === 'collapsed') {
      appendCollapsed(container, item.rows.length, item.rows, renderRow);
    } else if (item.row.kind === 'meta' || item.row.kind === 'hunk') {
      container.appendChild(renderFullRow(item.row));
    } else {
      container.appendChild(renderRow(item.row));
    }
  }
}

function renderSplitCell(row, side, tokens) {
  const kind = row ? row.kind : 'empty';
  const cell = el('span', 'diff-cell diff-cell--' + kind + ' diff-cell--' + side);
  const gutter = el('span', 'diff-gutter');
  const line = row ? (side === 'old' ? row.oldLine : row.newLine) : null;
  gutter.appendChild(el('span', 'diff-ln', lineNumber(line)));
  gutter.appendChild(el('span', 'diff-marker', row ? diffMarker(row.kind) : ' '));
  cell.appendChild(gutter);
  const code = el('span', 'diff-code');
  if (row) appendDiffCode(code, row, tokens);
  cell.appendChild(code);
  return cell;
}

/* Side-by-side is two files at once — the old side and the new side — so each
 * column carries its own multi-line state. */
function renderSplitRow(pair, oldHl, newHl) {
  if (pair.full) return renderFullRow(pair.left);
  const div = el('div', 'diff-row diff-row--split');
  div.appendChild(renderSplitCell(pair.left, 'old',
    pair.left ? highlightRow(pair.left, oldHl) : null));
  div.appendChild(renderSplitCell(pair.right, 'new',
    pair.right ? highlightRow(pair.right, newHl) : null));
  return div;
}

function renderSplitRows(container, rows, hl) {
  const oldHl = { lang: hl.lang, state: highlightState() };
  const newHl = { lang: hl.lang, state: highlightState() };
  const renderPair = (pair) => renderSplitRow(pair, oldHl, newHl);
  for (const item of collapsePairs(alignHunkRows(rows))) {
    if (item.type === 'collapsed') {
      appendCollapsed(container, item.pairs.length, item.pairs, renderPair);
    } else {
      container.appendChild(renderPair(item.pair));
    }
  }
}

/* One file: a sticky, collapsible header (path, +N -M) over its hunks. */
function renderDiffFile(file) {
  const section = el('section', 'diff-file-block');
  if (file.path) section.dataset.path = file.path;
  const head = el('button', 'diff-file-head');
  head.type = 'button';
  head.title = 'Collapse or expand this file';
  head.setAttribute('aria-expanded', 'true');
  head.appendChild(el('span', 'diff-file-caret', '▾'));
  head.appendChild(el('span', 'diff-file-name', file.path || '(unknown file)'));
  head.appendChild(el('span', 'diff-file-add', '+' + file.additions));
  head.appendChild(el('span', 'diff-file-del', '−' + file.deletions));
  const rowsEl = el('div', 'diff-file-rows');
  head.addEventListener('click', () => {
    rowsEl.hidden = !rowsEl.hidden;
    head.setAttribute('aria-expanded', rowsEl.hidden ? 'false' : 'true');
    head.classList.toggle('diff-file-head--collapsed', rowsEl.hidden);
    head.querySelector('.diff-file-caret').textContent = rowsEl.hidden ? '▸' : '▾';
  });
  section.appendChild(head);
  section.appendChild(rowsEl);
  // The language comes from the file path in the diff header; an unknown
  // extension is plain, so it renders exactly as before.
  const hl = { lang: highlightLang(file.path), state: highlightState() };
  for (const block of file.blocks) {
    if (block.type === 'meta') {
      rowsEl.appendChild(renderFullRow(block.row));
      continue;
    }
    if (block.header) rowsEl.appendChild(renderFullRow(block.header));
    if (effectiveDiffMode() === 'split') renderSplitRows(rowsEl, block.rows, hl);
    else renderUnifiedRows(rowsEl, block.rows, hl);
  }
  return section;
}

/* The diff itself. Every piece of model-written text is inserted as a TEXT
 * NODE (el() sets textContent; segments use createTextNode) — never innerHTML. */
function renderDiff(data) {
  state.diffLast = data;
  diffBody.textContent = '';
  renderDiffVerdict(data.review);
  renderDiffFiles(data.files);
  renderDiffViews();
  applyDiffPalette();
  if (!data.diff) {
    diffBody.appendChild(el('div', 'diff-row diff-row--ctx',
      data.reason ? '(' + data.reason + ')' : '(no changes in this range)'));
    return;
  }
  for (const file of parseDiff(data.diff)) {
    diffBody.appendChild(renderDiffFile(file));
  }
  if (data.truncated) {
    diffBody.appendChild(el('div', 'diff-row diff-row--meta diff-truncated',
      '… diff truncated — open the log or the worktree for the rest'));
  }
}

async function loadDiff() {
  const id = state.diffCardId;
  if (!id) return;
  diffBody.textContent = '…';
  const data = await api('/api/cards/' + encodeURIComponent(id)
    + '/diff?range=' + encodeURIComponent(state.diffRange));
  renderDiff(data);
}

/* Open the popup for one card (only one popup at a time: the activity popup
 * closes). `anchor` is the clicked card, so the panel opens next to it. */
async function openDiff(cardId, range, anchor) {
  if (!cardId) return;
  closeActivity();
  state.diffCardId = cardId;
  if (range) state.diffRange = range;
  const card = state.cards.find((n) => n.id === cardId);
  diffHeading.textContent = 'Diff — ' + (card ? card.title : cardId);
  diffPopup.hidden = false;
  markPanelPositioning(diffPopup);
  positionFloatingPanel(diffPopup, anchor, DIFF_WIDTH, DIFF_HEIGHT,
    loadPanelLayout('diff'));
  renderDiffRanges();
  state.diffLast = null;
  renderDiffViews();
  applyDiffPalette();
  await loadDiff();
}

function closeDiff() {
  state.diffCardId = null;
  diffPopup.hidden = true;
}

$('diff-close').addEventListener('click', closeDiff);
diffPaletteBtn.addEventListener('click', () => {
  state.diffPalette = state.diffPalette === 'colorblind' ? 'default' : 'colorblind';
  saveDiffPalette(state.diffPalette);
  applyDiffPalette();
});
makePanelDraggable(diffPopup, diffPopupHead, rememberPanelLayout(diffPopup, 'diff'));

/* ------------- Theme indicator (automatic dark mode, sun-based) ------------- */

// theme.js owns the dark/light decision (sun over your location when it is
// known, otherwise over Gothenburg, Sweden) and toggles the `dark` class on
// <html>. Here we just surface *why* in the top bar: a pill showing the
// current state plus today's sun times.

function renderThemeIndicator(info) {
  const pill = $('theme-indicator');
  if (!pill) return;
  const place = info.place || (window.VibeTheme && window.VibeTheme.place)
    || { name: 'Gothenburg, Sweden', timeZone: 'Europe/Stockholm' };
  const tz = place.timeZone || 'Europe/Stockholm';
  const opts = { timeZone: tz, hour: '2-digit', minute: '2-digit' };
  const fmt = (d) => (d instanceof Date && !isNaN(d) ? d.toLocaleTimeString('sv-SE', opts) : '—');
  pill.textContent = (info.dark ? '🌙 ' : '☀️ ') + place.name + ' ' + (info.dark ? 'night' : 'day');
  const where = (typeof place.lat === 'number' && typeof place.lon === 'number')
    ? ' (' + Math.abs(place.lat).toFixed(2) + '° ' + (place.lat >= 0 ? 'N' : 'S') + ', '
      + Math.abs(place.lon).toFixed(2) + '° ' + (place.lon >= 0 ? 'E' : 'W') + ')'
    : '';
  pill.title = 'Automatic dark mode — active when it is dark at ' + place.name + where + ' (' + tz + '). '
    + 'Your location is used when known; otherwise Gothenburg, Sweden is the standard. '
    + 'Today: sunrise ' + fmt(info.sunrise) + ', sunset ' + fmt(info.sunset) + '.';
}

document.addEventListener('vibe-theme', (e) => renderThemeIndicator(e.detail));

/* ---------------- Global wiring ---------------- */

$('error-dismiss').addEventListener('click', hideError);

function notTyping(e) {
  return e.target && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA'
    && e.target.tagName !== 'SELECT' && !e.target.isContentEditable;
}

/* The keys handled here are the ones listed in KEYBINDINGS (see the Keys
 * panel) — keep the two in step. */
function anyOverlayOpen() {
  return !editorOverlay.hidden || !logOverlay.hidden || !projectsOverlay.hidden;
}

document.addEventListener('keydown', (e) => {
  // Hold a letter and click a card: D opens its diff, C the live activity
  // popup (see buildCard). Tracked here so the Keys panel and the gesture
  // read the same key.
  if ((e.key === 'c' || e.key === 'C') && notTyping(e)) state.cKeyDown = true;
  if ((e.key === 'd' || e.key === 'D') && notTyping(e)) state.dKeyDown = true;
  if (e.key === 'Escape') {
    // A live query is cleared first: Esc on a search means "show me the board
    // again", not "close whatever overlay happens to be open".
    if (state.searchQuery !== '') {
      e.preventDefault();
      clearSearch();
      return;
    }
    // Modals first (they sit on top), then the floating popups.
    if (!logOverlay.hidden) closeLog();
    else if (!editorOverlay.hidden) closeEditor();
    else if (!projectsOverlay.hidden) closeProjects();
    else if (!diffPopup.hidden) closeDiff();
    else if (!activityPopup.hidden) closeActivity();
  } else if (e.key === '/' && !anyOverlayOpen() && notTyping(e)) {
    e.preventDefault();
    searchInput.focus();
  } else if ((e.key === 'n' || e.key === 'N') && !anyOverlayOpen() && notTyping(e)) {
    openEditor(null).catch(showError);
  }
});

document.addEventListener('keyup', (e) => {
  if (e.key === 'c' || e.key === 'C') state.cKeyDown = false;
  if (e.key === 'd' || e.key === 'D') state.dKeyDown = false;
});

// Alt-tabbing while a letter is held must not leave the board stuck in
// "a letter is held" mode — the keyup never arrives when the window loses
// focus.
window.addEventListener('blur', () => {
  state.cKeyDown = false;
  state.dKeyDown = false;
});

/* ---------------- Front-end build check ---------------- */

/* The dashboard is a long-lived tab: a browser happily keeps running the
 * app.js it loaded days ago, so every front-end fix looks like it did nothing
 * until the tab is reloaded by hand. Poll the server's asset fingerprint and
 * reload once it changes, so the tab always runs the code on disk. */
const BUILD_CHECK_MS = 5000;

async function checkBuild() {
  try {
    const { version } = await api('/api/version');
    if (!version) return;
    if (!state.buildVersion) { state.buildVersion = version; return; }
    if (state.buildVersion !== version) {
      state.buildVersion = version;
      window.location.reload();
    }
  } catch (_) { /* server restarting — try again on the next tick */ }
}

/* ---------------- Boot ---------------- */

renderThemeIndicator(window.VibeTheme ? window.VibeTheme.state : { dark: false, sunrise: null, sunset: null });
// Land back on the project selected last time in this browser.
state.selectedProject = loadSelectedProject();
// ...and on the notification choice made in the sidebar's Settings panel.
state.notificationsEnabled = loadNotificationsEnabled();
// ...and on the failure filter, so a board narrowed to one reason stays that way.
state.failureFilter = loadFailureFilter();
renderFailureFilter();
// Card stacks come from the server with the cards (see refresh), and per-column
// drop targets are wired once — renderBoard only replaces the columns'
// children, not the columns.
for (const col of COLUMNS) wireColumnDrop($('col-' + col.key), col.key);
loadProjects();
// The model list feeds the picker on every card, so load it before the first
// board render (the Models panel re-reads it when a role changes).
Promise.all([loadModels(), loadSettings()]).then(() => refresh()).then(migrateLocalStacks);
checkBuild();
setInterval(checkBuild, BUILD_CHECK_MS);
