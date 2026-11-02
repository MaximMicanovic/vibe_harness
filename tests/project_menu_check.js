/* Project menu check: "add project" lives at the bottom of the project dropdown.
 *
 * Usage: node tests/project_menu_check.js <path-to-app.js>
 *
 * Boots the served app.js under the shared DOM stub (tests/dom_stub.js) and
 * checks the spec:
 *
 *   - the top bar's project dropdown lists "All projects", then every project,
 *     and the "Add project…" entry LAST (the bottom of the menu);
 *   - choosing that entry opens the projects modal (where a project is added)
 *     and leaves the board's project filter alone — the menu snaps back to the
 *     project that was selected;
 *   - choosing a project still filters the board;
 *   - adding a project from the modal POSTs it and the new project shows up in
 *     the dropdown, with the "Add project…" entry still at the bottom.
 *
 * Exits 0 and prints JSON when everything holds; exits 1 with the failures.
 */
'use strict';
const { makeDom } = require('./dom_stub.js');

const APP_JS = process.argv[2];
if (!APP_JS) { console.error('usage: node tests/project_menu_check.js <app.js>'); process.exit(2); }

/* ---------- the server the stub answers for ---------- */
let projects = [
  { name: 'Alpha', path: '/tmp/alpha' },
  { name: 'Beta', path: '/tmp/beta' },
];
const calls = [];

function res(payload, status) {
  const code = status || 200;
  return {
    ok: code < 400, status: code, statusText: code < 400 ? 'OK' : 'Error',
    headers: { get: () => null },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

async function fetchStub(path, options) {
  const p = String(path);
  const method = (options && options.method) || 'GET';
  calls.push({ url: p, options: options || {} });
  if (p === '/api/projects' && method === 'POST') {
    const body = JSON.parse(options.body);
    projects = projects.concat([{ name: body.name, path: body.path }])
      .sort((a, b) => a.name.localeCompare(b.name));
    return res({ name: body.name, path: body.path }, 201);
  }
  if (p === '/api/projects') return res(projects);
  if (p === '/api/cards') return res([]);
  if (p.startsWith('/api/stacks')) return res([]);
  if (p.startsWith('/api/archives')) return res([]);
  if (p.startsWith('/api/models')) return res({ roles: {}, models: [], default_model: null });
  if (p.startsWith('/api/settings')) return res({ backend: 'vibe', backends: [] });
  if (p.startsWith('/api/version')) return res({ version: 'test' });
  if (p.startsWith('/api/stats')) return res({ total_tokens: 0, total_runtime_seconds: 0, avg_runtime_seconds: 0 });
  return res({});
}

const dom = makeDom({ fetch: fetchStub });
const X = dom.boot(APP_JS, ['state', 'loadProjects', 'openProjects', 'closeProjects']);
const { getEl, flush } = dom;

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

const menu = () => getEl('project-select');
const optionTexts = () => menu().children.map((o) => o.textContent);
const optionValues = () => menu().children.map((o) => o.value);
const isAddOption = (o) => /add project/i.test(o.textContent);
const addOption = () => menu().children.filter(isAddOption)[0] || null;

/* Fire the menu's change handler the way a browser does after a pick. */
function pick(value) {
  menu().value = value;
  for (const fn of (menu().listeners.change || [])) fn({ target: menu() });
}

async function main() {
  await flush();                       // let the boot loadProjects() land

  /* --- 1. the menu: All projects, each project, then "Add project…" last --- */
  const texts = optionTexts();
  check(texts.length === 4,
    'the project menu should list All projects + 2 projects + Add project…, got '
      + JSON.stringify(texts));
  check(texts[0] === 'All projects', 'the menu starts with All projects, got ' + JSON.stringify(texts[0]));
  check(texts[1] === 'Alpha' && texts[2] === 'Beta',
    'the menu lists the projects in order, got ' + JSON.stringify(texts));
  const add = addOption();
  check(!!add, 'the project menu should offer an "Add project…" entry, got ' + JSON.stringify(texts));
  check(!!add && menu().children[menu().children.length - 1] === add,
    'the "Add project…" entry belongs at the BOTTOM of the menu, got ' + JSON.stringify(texts));
  check(!!add && add.value !== '' && add.value !== 'Alpha' && add.value !== 'Beta',
    'the "Add project…" entry needs its own value, not a project name, got '
      + JSON.stringify(add && add.value));

  /* --- 2. picking it opens the projects modal, the filter stays put --- */
  const overlay = getEl('projects-overlay');
  check(overlay.hidden === true, 'the projects modal starts closed');
  X.state.selectedProject = '';
  if (add) {
    pick(add.value);
    await flush();
    check(overlay.hidden === false,
      'picking "Add project…" opens the projects modal (that is where a project is added)');
    check(X.state.selectedProject === '',
      'picking "Add project…" must not change the board filter, got '
        + JSON.stringify(X.state.selectedProject));
    check(menu().value === '',
      'the menu snaps back to the selected project after "Add project…", got '
        + JSON.stringify(menu().value));
  }

  /* --- 3. adding a project from that modal fills the menu --- */
  getEl('project-name').value = 'Gamma';
  getEl('project-path').value = '/tmp/gamma';
  const submit = { preventDefault() {} };
  for (const fn of (getEl('project-form').listeners.submit || [])) fn(submit);
  await flush();
  const post = calls.filter((c) => c.url === '/api/projects' && c.options.method === 'POST').pop();
  check(!!post, 'submitting the modal should POST /api/projects');
  if (post) {
    const body = JSON.parse(post.options.body);
    check(body.name === 'Gamma' && body.path === '/tmp/gamma',
      'the POST carries the typed name and path, got ' + post.options.body);
  }
  const after = optionTexts();
  check(after.indexOf('Gamma') !== -1,
    'the new project shows up in the menu, got ' + JSON.stringify(after));
  check(!!addOption() && menu().children[menu().children.length - 1] === addOption(),
    'the "Add project…" entry stays at the bottom after a project is added, got '
      + JSON.stringify(after));

  /* --- 4. picking a project still filters the board --- */
  X.closeProjects();
  pick('Beta');
  await flush();
  check(X.state.selectedProject === 'Beta',
    'picking a project still filters the board, got ' + JSON.stringify(X.state.selectedProject));
  check(overlay.hidden === true, 'picking a project does not open the projects modal');

  const result = { ok: failures.length === 0, failures };
  console.log(JSON.stringify(result, null, 2));
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, failures: ['check crashed: ' + (err && err.stack || err)] }, null, 2));
  process.exit(1);
});
