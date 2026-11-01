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

const state = {
  cards: [],
  stats: null,
  pollTimer: null,      // 2s cards+stats poll, active only while a card is live (running/reviewing/resolving/queued)
  buildVersion: null,   // server fingerprint of the front-end assets (see checkBuild)
  logTimer: null,       // 2s log refresh, active while the viewed card is running
  logCardId: null,
  editorCardId: null,   // null => editor is creating a new card
  templates: [],
  templatesLoaded: false,
  projects: [],
  selectedProject: '',   // '' = All projects
  expandedTitles: new Set(),  // card ids whose card title shows the full name
  stacks: [],            // [{ id, col, ids: [cardId bottom→top] }] — visual card stacks per column
  dragCardId: null,      // card id currently being dragged (HTML5 drag & drop: drafts link, others stack)
  renderPending: false,  // a board rebuild deferred while a drag is in progress (see renderBoard)
  openStackId: null,     // stack id currently fanned out by hover
  archives: [],          // archives from GET /api/archives
  archivePick: null,     // null | { kind: 'card'|'cards'|'project', id, ids, name, label } — pick an archive to move into
  archiveExpanded: new Set(),  // archive ids whose card list is shown
  sidebarPanel: null,    // null | 'models' | 'archive' — the open sidebar panel
  sidebarArchiveOpen: new Set(),  // archive ids expanded in the sidebar's archive browser
  sidebarProjectOpen: new Set(),  // project names expanded in the archive browser
  sidebarArchiveProjects: new Set(),  // '<archiveId>::<project>' keys the user folded away
  archiveProjects: new Map(),  // archive id -> its project groups (GET /api/archives/<id>), cached for the sidebar
  models: null,          // GET /api/models: { roles, models, default_model }
  settings: null,        // GET /api/settings: { backend, backends }
  prevStatuses: null,    // Map cardId -> status from the previous refresh (null = first load)
  notificationsEnabled: true,  // sidebar Settings toggle, persisted in localStorage
  unreadNotifications: 0,      // finishes announced while the tab was hidden (title badge)
};

/* ---------------- DOM refs ---------------- */

const $ = (id) => document.getElementById(id);
const emptyBoard = $('empty-board');
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
const manageProjectsBtn = $('manage-projects-btn');
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
const btnArchive = $('btn-archive');
const btnLog = $('btn-log');
const btnDelete = $('btn-delete');
const acceptHint = $('accept-hint');

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
    throw new Error(detail);
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

/* A draft whose run was queued because a merge conflict is being applied in
 * its repo — the card spins until the conflict resolves and the run starts. */
function isQueued(card) {
  const s = card.session || {};
  return s.queued === true;
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

function buildCard(cardData) {
  const card = el('article', 'card card--' + cardData.status);
  card.dataset.cardId = cardData.id;
  card.style.setProperty('--tilt', tiltFor(cardData.id));

  const head = el('div', 'card-head');
  if (cardData.status === 'running' || cardData.status === 'reviewing' || isResolving(cardData) || isQueued(cardData)) head.appendChild(el('span', 'spinner'));
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
  if (Array.isArray(cardData.depends_on) && cardData.depends_on.length) {
    const names = cardData.depends_on.map(depName);
    const dc = chip('depends', '⛓ runs after ' + names.join(', '));
    dc.title = 'This card runs automatically when ' + names.join(', ')
      + ' completes (done/accepted) — a red string links the cards. Remove the link in the editor.';
    chips.appendChild(dc);
  }
  if (s) {
    if (Array.isArray(s.waiting_for) && s.waiting_for.length) {
      const wc = chip('waiting', '⏳ waiting for ' + s.waiting_for.map(depName).join(', '));
      wc.title = s.queue_reason || 'waiting for dependencies to complete — the run starts automatically';
      chips.appendChild(wc);
    }
    if (s.queued === true) {
      const qc = chip('merge-conflict', '⏳ waiting for merge conflict to resolve');
      qc.title = s.queue_reason || 'run queued until the in-progress merge conflict resolves';
      chips.appendChild(qc);
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
      lc.title = 'Live token usage of the current session: ' + fmtTokens(s.live_tokens)
        + ' tok — updates while it runs' + tokenTypesSuffix(s.live_token_breakdown);
      chips.appendChild(lc);
    } else if (typeof s.tokens === 'number') {
      // Recorded tokens of the last session: same little bubble, carrying
      // cached, input, output in one chip when the breakdown is known.
      const inline = tokenTypesInline(s.token_breakdown);
      const tc = chip('tokens', inline || fmtTokens(s.tokens) + ' tok');
      tc.title = 'Token usage of the last session: ' + fmtTokens(s.tokens) + ' tok'
        + tokenTypesSuffix(s.token_breakdown);
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
    if (s.model) chips.appendChild(chip('model', s.model));
    if (cardData.auto_accept) chips.appendChild(chip('auto-accept', 'auto-accept on'));
  }
  if (chips.childNodes.length > 0) card.appendChild(chips);

  card.addEventListener('click', () => { openEditor(cardData.id).catch(showError); });

  // Drag a DRAFT card onto another card to link them: the dragged draft runs
  // automatically once the card it is dropped on completes (done/accepted).
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
function buildStack(stack, byId) {
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

/* Cards visible under the current top-bar project filter ('' = all projects). */
function visibleCards() {
  return state.selectedProject === ''
    ? state.cards
    : state.cards.filter((n) => (n.project || '') === state.selectedProject);
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
      const key = child.dataset && child.dataset.stackId ? 'stack:' + child.dataset.stackId
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
    const items = stackItems.concat(cardItems).sort((a, b) => b.ts - a.ts);
    // Place the items in sorted order without needless DOM moves. appendChild
    // on a node that is already in position still removes and re-inserts it,
    // and a re-inserted node restarts its CSS transitions — so re-appending
    // every card on every 2 s poll made the whole board twitch (the "shaking"
    // board). Walk a cursor and only touch the DOM when a node is out of place.
    let cursor = wrap.firstElementChild;
    for (const item of items) {
      const key = item.kind === 'stack' ? 'stack:' + item.stack.id : 'card:' + item.card.id;
      // Everything the item renders from: a change rebuilds the node.
      const sig = item.kind === 'stack'
        ? item.stack.ids.join('|') + '#' + (item.stack.title || '') + '#' + item.stack.ids.map((id) => JSON.stringify(byId.get(id))).join('|')
        : JSON.stringify(item.card) + (state.expandedTitles.has(item.card.id) ? '#expanded' : '');
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
        const stackEl = buildStack(item.stack, byId);
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
        const card = buildCard(item.card);
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
  emptyBoard.hidden = total > 0;
  renderStrings();
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
  const cardOf = (id) => board.querySelector('.card[data-card-id="' + CSS.escape(id) + '"]');
  for (const n of visible) {
    for (const dep of n.depends_on || []) {
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
  state.selectedProject = projectSelect.value;
  // Remember the selection for the next browser session…
  saveSelectedProject(state.selectedProject);
  // …and working on a concrete project here makes it the last used one, so
  // the next new card defaults to it. ("All projects" is a view filter, not a
  // project use, and does not touch the last-used memory.)
  if (state.selectedProject) saveLastProjectDefault(state.selectedProject);
  refresh();
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

manageProjectsBtn.addEventListener('click', () => { openProjects().catch(showError); });
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
  models: { title: 'Models', render: renderModelsPanel },
  archive: { title: 'Archive', render: renderArchivePanel },
  split: { title: 'Split', render: renderSplitPanel },
  settings: { title: 'Settings', render: renderSettingsPanel },
};

function openSidebarPanel(key) {
  const panel = SIDEBAR_PANELS[key];
  if (!panel) return;
  if (state.sidebarPanel === key && !sidebarPanel.hidden) { closeSidebar(); return; }
  state.sidebarPanel = key;
  sidebarTitle.textContent = panel.title;
  sidebarPanel.hidden = false;
  for (const btn of sidebarRail.querySelectorAll('.rail-btn')) {
    btn.setAttribute('aria-expanded', btn.dataset.panel === key ? 'true' : 'false');
  }
  panel.render().catch(showError);
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

/* --- models: which model each kind of session runs on --- */

/* Which backend runs sessions, cached for the stats bar and the card chips. */
async function loadSettings() {
  try {
    state.settings = await api('/api/settings');
  } catch (_) {
    state.settings = null;  // assume the default backend
  }
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

async function saveRoleModel(role, model) {
  const patch = {};
  patch[role] = model;
  const data = await api('/api/models', { method: 'PUT', body: JSON.stringify({ models: patch }) });
  state.models = data;
  await renderModelsPanel();
}

/* --- settings: browser-side preferences --- */

/* These are per-browser choices (localStorage), not server settings: they
 * describe what this tab should do, not how the harness runs sessions. */
async function renderSettingsPanel() {
  sidebarBody.textContent = '';

  const row = el('label', 'setting-row');
  const box = el('input');
  box.type = 'checkbox';
  box.id = 'notify-toggle';
  box.checked = state.notificationsEnabled;
  box.title = 'Show a desktop notification when a card finishes while you are in another window';
  box.addEventListener('change', () => setNotificationsEnabled(box.checked));
  row.appendChild(box);
  row.appendChild(el('span', 'setting-label', 'Desktop notifications'));
  sidebarBody.appendChild(row);

  const permission = notificationPermission();
  sidebarBody.appendChild(el('p', 'sidebar-empty',
    permission === 'denied'
      ? 'This browser blocks notifications for the dashboard, so a short beep plays instead. '
        + 'Allow notifications in the site settings to get the desktop popup back.'
      : permission === 'unsupported'
        ? 'This browser has no Notification API, so a short beep plays instead.'
        : 'A card that finishes while you are in another window pops up a desktop notification. '
          + 'The browser asks for permission on your first click.'));
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

/* ---------------- Polling ---------------- */

function anyLive() {
  return state.cards.some((n) => n.status === 'running' || n.status === 'reviewing' || isResolving(n) || isQueued(n));
}

function updatePolling() {
  if (anyLive() && !state.pollTimer) {
    state.pollTimer = setInterval(refresh, 2000);
  } else if (!anyLive() && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
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
    announceFinished(diffCardStatuses(state.cards));
    renderStats();
    renderBoard();
    updatePolling();
  } catch (err) {
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
  hideError();
  if (cardId) {
    const card = await api('/api/cards/' + encodeURIComponent(cardId));
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
    // Only accepted cards can be archived.
    btnArchive.hidden = card.status !== 'accepted';
    btnLog.hidden = !card.session;
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
    acceptHint.hidden = true;
    btnArchive.hidden = true;
    btnLog.hidden = true;
    await loadTemplates();
    cardTitle.focus();
  }
  editorOverlay.hidden = false;
}

function closeEditor() {
  editorOverlay.hidden = true;
  state.editorCardId = null;
}

async function saveCard() {
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
    return api('/api/cards/' + encodeURIComponent(state.editorCardId), {
      method: 'PUT',
      body: JSON.stringify({ title: title, body: body, auto_run: autoRun, auto_accept: autoAccept, project: project, workdir: workdir }),
    });
  }
  const payload = { title: title, body: body, auto_run: autoRun, auto_accept: autoAccept, project: project, workdir: workdir };
  if (templateSelect.value) payload.template = templateSelect.value;
  return api('/api/cards', { method: 'POST', body: JSON.stringify(payload) });
}

// Remember the auto-accept checkbox setting as soon as it changes.
cardAutoAccept.addEventListener('change', () => saveAutoAcceptDefault(cardAutoAccept.checked));

btnSave.addEventListener('click', () => action(async () => {
  await saveCard();
  closeEditor();
}));

// Archive the open card (only offered for accepted cards): pick an archive.
btnArchive.addEventListener('click', () => {
  if (!state.editorCardId) return;
  openArchives({ kind: 'card', id: state.editorCardId, label: cardTitle.value.trim() || state.editorCardId })
    .catch(showError);
});

btnRun.addEventListener('click', () => action(async () => {
  // Save first: edits are persisted, and a changed spec resets the card to draft.
  const saved = await saveCard();
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

btnDelete.addEventListener('click', () => action(async () => {
  if (!window.confirm('Delete this card and its session files?')) return;
  await api('/api/cards/' + encodeURIComponent(state.editorCardId), { method: 'DELETE' });
  closeEditor();
}));

btnLog.addEventListener('click', () => {
  if (state.editorCardId) openLog(state.editorCardId);
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

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!logOverlay.hidden) closeLog();
    else if (!editorOverlay.hidden) closeEditor();
    else if (!projectsOverlay.hidden) closeProjects();
  } else if ((e.key === 'n' || e.key === 'N') && editorOverlay.hidden && logOverlay.hidden
             && projectsOverlay.hidden && notTyping(e)) {
    openEditor(null).catch(showError);
  }
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
