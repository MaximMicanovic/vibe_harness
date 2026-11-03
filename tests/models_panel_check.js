/* Models panel check: one list, both platforms.
 *
 * Usage: node tests/models_panel_check.js <path-to-app.js>
 *
 * Boots the served app.js under the shared DOM stub (tests/dom_stub.js) and
 * checks the spec:
 *
 *   - the Models panel has no Backend dropdown: picking a model IS picking the
 *     platform, so there is no second control to contradict it;
 *   - the picker lists the Vibe models and the Claude aliases, each tagged
 *     with the backend that runs it (an optgroup per platform);
 *   - a model whose CLI is not installed is disabled, with the reason;
 *   - saving a role sends {role, model, backend};
 *   - the token counter follows the resolved backend, not settings.backend.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const { makeDom } = require('./dom_stub.js');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: models_panel_check.js <app.js>'); process.exit(2); }

const failures = [];
function check(ok, msg) { if (!ok) failures.push(msg); }

const EXPORTS = ['state', 'renderModelsPanel', 'backendHasLiveTokens'];

const MODELS = {
  roles: [
    { key: 'worker', label: 'Worker', session: 'run', available: true,
      description: 'Implements the card and its fixer rounds (run-N).',
      model: '', backend: '', effective: '', effective_backend: 'vibe',
      inherited: true, inherited_from: null },
    { key: 'review', label: 'Review', session: 'review', available: true,
      description: 'Reviews the diff and the test output (review-N).',
      model: 'sonnet', backend: 'claude', effective: 'sonnet',
      effective_backend: 'claude', inherited: false, inherited_from: 'review' },
    { key: 'resolver', label: 'Merge conflict', session: 'resolve', available: true,
      description: 'Resolves a conflicted merge when a card is accepted (resolve-N).',
      model: '', backend: '', effective: '', effective_backend: 'vibe',
      inherited: true, inherited_from: null },
    { key: 'orchestrator', label: 'Orchestrator', session: null, available: false,
      description: 'No orchestrator session exists yet.',
      model: '', backend: '', effective: '', effective_backend: 'vibe',
      inherited: true, inherited_from: null },
  ],
  models: [
    { alias: 'deepseek-v4-1-flash-1017', label: 'DeepSeek V4.1 Flash 1017',
      backend: 'vibe', thinking: null, available: true, reason: null },
    { alias: 'glm-5-3', label: 'GLM-5.3', backend: 'vibe', thinking: 'high',
      available: true, reason: null },
    { alias: 'mistral-large-4-0-3', label: 'Mistral Large 4', backend: 'vibe',
      thinking: 'high', available: true, reason: null },
    { alias: 'zai-glm-5-3-flash', label: 'GLM-5.3 Flash', backend: 'vibe',
      thinking: null, available: true, reason: null },
    { alias: 'sonnet', label: 'Claude Sonnet', backend: 'claude', thinking: null,
      available: true, reason: null },
    { alias: 'opus', label: 'Claude Opus', backend: 'claude', thinking: null,
      available: true, reason: null },
    { alias: 'haiku', label: 'Claude Haiku', backend: 'claude', thinking: null,
      available: false, reason: 'claude is not on PATH' },
  ],
  default_model: '',
  resolved_backend: 'vibe',
};

const SETTINGS = {
  backend: 'vibe',
  backends: [
    { key: 'vibe', label: 'Mistral Vibe', binary: 'vibe', available: true },
    { key: 'claude', label: 'Claude Code', binary: 'claude', available: false },
  ],
  accept_modes: [{ key: 'local', label: 'Merge locally', description: 'merge' }],
  accept_mode: 'local',
  failure_kinds: [],
};

function res(payload, status) {
  const code = status || 200;
  return {
    ok: code < 400, status: code, statusText: code < 400 ? 'OK' : 'Error',
    headers: { get: () => null },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

const puts = [];

async function fetchStub(path, options) {
  const p = String(path);
  const method = (options && options.method) || 'GET';
  if (p.startsWith('/api/models')) {
    if (method === 'PUT') {
      puts.push(JSON.parse(options.body));
      return res(MODELS);
    }
    return res(MODELS);
  }
  if (p.startsWith('/api/settings')) return res(SETTINGS);
  if (p === '/api/cards') return res([]);
  if (p.startsWith('/api/stacks')) return res([]);
  if (p.startsWith('/api/archives')) return res([]);
  if (p.startsWith('/api/projects')) return res([]);
  if (p.startsWith('/api/version')) return res({ version: 'test' });
  if (p.startsWith('/api/stats')) {
    return res({ total_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0 });
  }
  return res({});
}

/* Every node under `root`, depth first. */
function walk(root, out) {
  out = out || [];
  for (const child of root.children || []) { out.push(child); walk(child, out); }
  return out;
}

function byId(root, id) {
  return walk(root).find((n) => n.id === id) || null;
}

/* The options of a select, including the ones inside its optgroups. */
function optionsOf(select) {
  const out = [];
  for (const child of select.children || []) {
    if (child.tagName === 'OPTGROUP') out.push(...(child.children || []));
    else out.push(child);
  }
  return out;
}

async function main() {
  const dom = makeDom({ fetch: fetchStub });
  const X = dom.boot(APP_JS, EXPORTS);
  await dom.flush();

  await X.renderModelsPanel();
  await dom.flush();

  const body = dom.getEl('sidebar-body');
  const nodes = walk(body);

  /* 1. No Backend dropdown anywhere in the panel. */
  check(!nodes.some((n) => n.id === 'backend-select'),
    'the Models panel still renders a #backend-select dropdown');
  check(!nodes.some((n) => n.tagName === 'SELECT'
    && (n.id || '').indexOf('backend') !== -1),
    'the Models panel still renders a backend select');

  /* 2. One list, both platforms: the four Vibe models and the three Claude
   *    aliases, each tagged with the backend that runs it. */
  const select = byId(body, 'model-worker');
  check(!!select, 'the worker role has no model picker');
  if (select) {
    const options = optionsOf(select);
    const aliases = options.map((o) => o.value);
    for (const alias of ['deepseek-v4-1-flash-1017', 'glm-5-3',
      'mistral-large-4-0-3', 'zai-glm-5-3-flash', 'sonnet', 'opus', 'haiku']) {
      check(aliases.indexOf(alias) !== -1, 'the picker is missing ' + alias);
    }
    const sonnet = options.find((o) => o.value === 'sonnet');
    check(sonnet && sonnet.dataset.backend === 'claude',
      'the Claude alias is not tagged with its backend');
    const large = options.find((o) => o.value === 'mistral-large-4-0-3');
    check(large && large.dataset.backend === 'vibe',
      'the Vibe model is not tagged with its backend');
    check(sonnet && sonnet.textContent.indexOf('Claude Sonnet') !== -1,
      'the Claude alias is not labelled with its name');

    /* The platform is visible: one optgroup per backend. */
    const groups = (select.children || []).filter((c) => c.tagName === 'OPTGROUP');
    const labels = groups.map((g) => g.label);
    check(labels.indexOf('Mistral Vibe') !== -1,
      'the Vibe models are not grouped under their platform, got ' + JSON.stringify(labels));
    check(labels.indexOf('Claude Code') !== -1,
      'the Claude models are not grouped under their platform, got ' + JSON.stringify(labels));

    /* 3. A model whose CLI is not installed is disabled, with the reason. */
    const haiku = options.find((o) => o.value === 'haiku');
    check(haiku && haiku.disabled === true,
      'a model of a missing CLI is still selectable');
    check(haiku && String(haiku.title || '').indexOf('not on PATH') !== -1,
      'a disabled model does not say why');

    /* 4. Saving a role sends the model AND the backend it runs on. */
    select.value = 'sonnet';
    const onChange = (select.listeners.change || [])[0];
    check(typeof onChange === 'function', 'the model picker has no change handler');
    if (onChange) {
      onChange();
      await dom.flush();
      const put = puts[puts.length - 1];
      check(put && put.role === 'worker' && put.model === 'sonnet'
        && put.backend === 'claude',
        'saving a role did not send {role, model, backend}, got ' + JSON.stringify(put));
    }
  }

  /* 5. The token counter follows the resolved backend, not settings.backend. */
  X.state.settings = { backend: 'vibe' };
  X.state.models = { resolved_backend: 'claude' };
  check(X.backendHasLiveTokens() === false,
    'the token counter is shown for a Claude run');
  X.state.models = { resolved_backend: 'vibe' };
  check(X.backendHasLiveTokens() === true,
    'the token counter is hidden for a Vibe run');
  X.state.models = null;
  check(X.backendHasLiveTokens() === true,
    'the token counter is hidden before the models load');

  if (failures.length) {
    console.log(JSON.stringify({ ok: false, failures }));
    process.exit(1);
  }
  console.log(JSON.stringify({ ok: true, failures: [] }));
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, failures: [String(err && err.stack || err)] }));
  process.exit(1);
});
