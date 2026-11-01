/* In-memory stand-in for the server's stack API, for the node harnesses that
 * boot app.js under a DOM stub.
 *
 * Stacks are server state now (app/stacks.py + the /api/stacks routes in
 * app/main.py): the board reads them with GET /api/stacks and every mutation
 * goes through the API. A harness that answers every fetch with `{}` would
 * therefore see its piles vanish on the next refresh, so the harnesses share
 * this small implementation of the same endpoints.
 *
 * Usage:
 *
 *   const { makeStacksApi } = require('./stacks_api_stub.js');
 *   const stacksApi = makeStacksApi(() => X.state.cards, fallbackFetch);
 *   sandbox.fetch = stacksApi;
 *
 * `notes` is a function returning the current notes array (the stub needs each
 * note's status to know which column it renders in); `fallback` handles every
 * path that is not a stack route.
 */
'use strict';

// Mirrors COLUMN_OF_STATUS in app/stacks.py: "stopped" cards sit in Failed.
const COLUMN_OF_STATUS = {
  draft: 'draft',
  running: 'running',
  reviewing: 'reviewing',
  done: 'done',
  accepted: 'accepted',
  failed: 'failed',
  stopped: 'failed',
};
const MIN_MEMBERS = 2;

function makeStacksApi(notes, fallback) {
  const stacks = [];
  let seq = 0;

  const noteList = () => (typeof notes === 'function' ? notes() : notes) || [];
  const columnOf = (id) => {
    const note = noteList().find((n) => n && n.id === id);
    return note ? COLUMN_OF_STATUS[note.status] || null : null;
  };
  const now = () => new Date().toISOString();

  const res = (body, status) => {
    const code = status || 200;
    return {
      ok: code < 400,
      status: code,
      statusText: code < 400 ? 'OK' : 'Error',
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  };
  const fail = (detail, status) => res({ detail }, status || 400);

  /* Drop members that vanished or changed column; dissolve piles below two. */
  function prune() {
    const seen = new Set();
    const out = [];
    for (const s of stacks) {
      const ids = [];
      for (const id of s.ids) {
        if (seen.has(id) || columnOf(id) !== s.col) continue;
        seen.add(id);
        ids.push(id);
      }
      if (ids.length >= MIN_MEMBERS) out.push(Object.assign({}, s, { ids }));
    }
    stacks.length = 0;
    stacks.push.apply(stacks, out);
    return stacks;
  }

  function checkMembers(ids, col) {
    const seen = [];
    for (const id of ids) if (seen.indexOf(id) === -1) seen.push(id);
    if (seen.length < MIN_MEMBERS) return { error: 'a stack needs at least ' + MIN_MEMBERS + ' cards' };
    for (const id of seen) {
      const actual = columnOf(id);
      if (actual === null) return { error: 'note ' + id + ' has no board column' };
      if (actual !== col) return { error: 'note ' + id + ' is in the ' + actual + ' column, not ' + col };
    }
    return { ids: seen };
  }

  /* Take `ids` out of every pile except `keep`; dissolve emptied piles. */
  function detach(ids, keep) {
    const out = [];
    for (const s of stacks) {
      if (s.id === keep) { out.push(s); continue; }
      const remaining = s.ids.filter((i) => ids.indexOf(i) === -1);
      if (remaining.length === s.ids.length) out.push(s);
      else if (remaining.length >= MIN_MEMBERS) out.push(Object.assign({}, s, { ids: remaining, updated_at: now() }));
    }
    stacks.length = 0;
    stacks.push.apply(stacks, out);
  }

  const find = (id) => stacks.find((s) => s.id === id) || null;

  function create(body) {
    const col = body.col || columnOf((body.card_ids || [])[0]);
    if (!col) return fail('unknown column');
    const checked = checkMembers(body.card_ids || [], col);
    if (checked.error) return fail(checked.error);
    detach(checked.ids, null);
    const stack = {
      id: 'st-' + (++seq),
      col,
      title: String(body.title || '').trim(),
      ids: checked.ids,
      created_at: now(),
      updated_at: now(),
    };
    stacks.push(stack);
    return res(stack);
  }

  function update(id, body) {
    const stack = find(id);
    if (!stack) return fail('stack ' + id + ' not found', 404);
    if (body.card_ids !== undefined) {
      const checked = checkMembers(body.card_ids, stack.col);
      if (checked.error) return fail(checked.error);
      stack.ids = checked.ids;
      detach(checked.ids, id);
    }
    if (body.title !== undefined) stack.title = String(body.title || '').trim();
    stack.updated_at = now();
    return res(stack);
  }

  function addNotes(id, body) {
    const stack = find(id);
    if (!stack) return fail('stack ' + id + ' not found', 404);
    const ids = body.card_ids || [];
    for (const nid of ids) {
      const actual = columnOf(nid);
      if (actual !== stack.col) return fail('note ' + nid + ' is in the ' + actual + ' column, not ' + stack.col);
    }
    detach(ids, id);
    const target = find(id);
    for (const nid of ids) if (target.ids.indexOf(nid) === -1) target.ids.push(nid);
    target.updated_at = now();
    return res(target);
  }

  function removeNote(noteId) {
    const stack = stacks.find((s) => s.ids.indexOf(noteId) !== -1);
    if (!stack) return fail('note ' + noteId + ' is not in a stack', 404);
    stack.ids = stack.ids.filter((i) => i !== noteId);
    stack.updated_at = now();
    const dissolved = stack.ids.length < MIN_MEMBERS;
    if (dissolved) stacks.splice(stacks.indexOf(stack), 1);
    return res({ stack: dissolved ? null : stack, dissolved, note_id: noteId });
  }

  async function fetchStub(path, options) {
    const url = String(path);
    const method = ((options && options.method) || 'GET').toUpperCase();
    const body = options && options.body ? JSON.parse(options.body) : {};
    let m;

    if (url === '/api/stacks' && method === 'GET') return res(prune());
    if (url === '/api/stacks' && method === 'POST') return create(body);
    if ((m = url.match(/^\/api\/stacks\/([^/]+)\/notes$/)) && method === 'POST') return addNotes(m[1], body);
    if ((m = url.match(/^\/api\/stacks\/([^/]+)\/notes\/([^/]+)$/)) && method === 'DELETE') return removeNote(m[2]);
    if ((m = url.match(/^\/api\/stacks\/([^/]+)$/))) {
      if (method === 'GET') {
        const stack = find(m[1]);
        return stack ? res(stack) : fail('stack ' + m[1] + ' not found', 404);
      }
      if (method === 'PATCH') return update(m[1], body);
      if (method === 'DELETE') {
        const stack = find(m[1]);
        if (!stack) return fail('stack ' + m[1] + ' not found', 404);
        stacks.splice(stacks.indexOf(stack), 1);
        return res({ deleted: m[1], cards: stack.ids });
      }
    }
    if ((m = url.match(/^\/api\/cards\/([^/]+)\/unstack$/)) && method === 'POST') return removeNote(m[1]);

    if (fallback) return fallback(path, options);
    return res({});
  };

  /* Seed the "server" with piles a scenario wants to start from, and read the
   * current ones back (the board only ever sees them through GET). */
  fetchStub.seed = (list) => {
    stacks.length = 0;
    for (const s of list || []) {
      stacks.push({
        id: s.id || 'st-' + (++seq),
        col: s.col,
        title: s.title || '',
        ids: (s.ids || []).slice(),
        created_at: s.created_at || now(),
        updated_at: s.updated_at || now(),
      });
    }
    return stacks;
  };
  fetchStub.dump = () => stacks.map((s) => Object.assign({}, s, { ids: s.ids.slice() }));
  return fetchStub;
}

module.exports = { makeStacksApi, COLUMN_OF_STATUS, MIN_MEMBERS };
