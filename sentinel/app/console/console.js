/* Sentinel console.
 *
 * TWO RULES THIS FILE MUST NEVER BREAK:
 *
 * 1. Never innerHTML. Every string shown here — a capability `reason`,
 *    a tool name, an agent name, a flow id — was written by the very
 *    agent asking for power. Rendering that as markup would put stored
 *    XSS in the one page that holds the kill switch. Text goes in via
 *    textContent, always, no exceptions.
 *
 * 2. Never show stale state as if it were live. If a poll fails, say so
 *    loudly and stop pretending: an operator who thinks they are looking
 *    at "no pending requests" when they are really looking at a dead
 *    socket is worse off than one who knows the console is blind.
 */

const POLL_MS = 2000;
let killEngaged = false;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      'content-type': 'application/json',
      // The CSRF guard (app/actor.py). A cross-origin page cannot send
      // this header without a preflight that Sentinel never answers.
      'x-sentinel-console': '1',
      ...(opts.headers || {}),
    },
  });
  if (!res.ok && opts.method) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(
      Array.isArray(body.detail) ? `${body.detail.length} problem(s)`
        : (body.detail || `${res.status} ${res.statusText}`));
    err.detail = body.detail;   // the full error list, for panels that render it
    throw err;
  }
  if (!res.ok) throw new Error(`${res.status}`);
  return res.status === 204 ? null : res.json();
}

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};

const ago = (iso) => {
  if (!iso) return 'never';
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
};

const until = (iso) => {
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (s <= 0) return 'lapsed';
  return s < 60 ? `${Math.floor(s)}s left` : `${Math.floor(s / 60)}m left`;
};

const clock = (iso) => new Date(Date.parse(iso)).toLocaleTimeString();

/* --- pending requests: the decision panel ------------------------------- */

function renderPending(rows, auditByFlow) {
  const box = document.getElementById('pending');
  box.replaceChildren();
  document.getElementById('pending-count').textContent = rows.length;
  document.getElementById('pending-empty').classList.toggle('hidden', rows.length > 0);

  for (const r of rows) {
    const card = el('div', 'req');
    card.append(el('div', 'tool', r.tool));

    const meta = el('div', 'meta');
    meta.append(
      el('span', null, r.agent),
      el('span', null, '  ·  flow '),
      el('code', null, r.flow_id),
      el('span', null, `  ·  asked ${ago(r.requested_at)} · ${until(r.expires_at)}`),
    );
    card.append(meta, el('div', 'reason', r.reason));

    // Context the phase doc asks for: what else this flow has been up to.
    const recent = (auditByFlow[r.flow_id] || []).slice(0, 3);
    if (recent.length) {
      const ctx = el('div', 'ctx', 'recent on this flow: ');
      recent.forEach((e, i) => {
        if (i) ctx.append(el('span', null, ', '));
        ctx.append(el('span', null, `${e.event_type} ${e.tool || ''}`.trim()));
      });
      card.append(ctx);
    }

    const acts = el('div', 'acts');
    const decide = (label, cls, fn) => {
      const b = el('button', cls, label);
      b.onclick = async () => {
        acts.querySelectorAll('button').forEach((x) => (x.disabled = true));
        try {
          await fn();
        } catch (e) {
          const err = el('div', 'ctx', `refused: ${e.message}`);
          card.append(err);
          acts.querySelectorAll('button').forEach((x) => (x.disabled = false));
        }
        refresh();
      };
      return b;
    };
    const grant = (mins) => () =>
      api(`/v1/capability-requests/${r.request_id}/grant`, {
        method: 'POST', body: JSON.stringify({ ttl_minutes: mins }),
      });

    acts.append(
      decide('Grant 5m', 'grant', grant(5)),
      decide('Grant 1h', 'grant', grant(60)),
      decide('Deny', 'deny', () =>
        api(`/v1/capability-requests/${r.request_id}/deny`, {
          method: 'POST', body: JSON.stringify({ reason: 'denied at the console' }),
        })),
    );
    if (killEngaged) {
      acts.querySelectorAll('.grant').forEach((b) => (b.disabled = true));
      acts.append(el('span', 'ctx', 'kill switch engaged — no new grants'));
    }
    card.append(acts);
    box.append(card);
  }
}

/* --- flows + audit ------------------------------------------------------ */

function renderFlows(rows) {
  const box = document.getElementById('flows');
  box.replaceChildren();
  document.getElementById('flow-count').textContent = rows.length;
  document.getElementById('flows-empty').classList.toggle('hidden', rows.length > 0);

  for (const f of rows) {
    const row = el('div', 'row');
    row.append(el('span', 'id', f.id), el('span', 't', f.agent));
    const tail = el('span', 'tail');
    if (f.live_grants) tail.append(el('span', 'live', `${f.live_grants} live`), el('span', null, ' · '));
    if (f.pending_requests) tail.append(el('span', null, `${f.pending_requests} pending · `));
    tail.append(el('span', null, `seen ${ago(f.last_seen)}`));
    row.append(tail);
    box.append(row);
  }
}

function renderAudit(rows) {
  const box = document.getElementById('audit');
  box.replaceChildren();
  for (const e of rows) {
    const row = el('div', 'row');
    row.append(
      el('span', 't', clock(e.ts)),
      el('span', `tag ${e.event_type}`, e.event_type),
      el('span', 'id', e.tool || e.flow_id || '—'),
    );
    const why = (e.details && (e.details.reason || e.details.cause)) || '';
    row.append(el('span', 'tail', why || (e.actor ? `by ${e.actor}` : '')));
    box.append(row);
  }
}

function renderKill(ks) {
  killEngaged = ks.engaged;
  const bar = document.getElementById('killbar');
  bar.classList.toggle('engaged', ks.engaged);
  document.getElementById('kill-state').textContent =
    ks.engaged ? 'KILL SWITCH ENGAGED — all capabilities revoked' : 'kill switch: off';
  document.getElementById('kill-detail').textContent = ks.engaged
    ? `by ${ks.engaged_by} at ${clock(ks.engaged_at)}${ks.reason ? ' — ' + ks.reason : ''}`
    : '';
  document.getElementById('kill-btn').classList.toggle('hidden', ks.engaged);
  document.getElementById('release-btn').classList.toggle('hidden', !ks.engaged);
  if (ks.engaged) armKill(false);
}

/* Two-step confirmation: engaging the kill switch permanently revokes
   every live grant, so it must not be one stray click away. */
function armKill(armed) {
  document.getElementById('kill-btn').classList.toggle('hidden', armed || killEngaged);
  document.getElementById('kill-confirm').classList.toggle('hidden', !armed);
  document.getElementById('kill-cancel').classList.toggle('hidden', !armed);
}

document.getElementById('kill-btn').onclick = () => armKill(true);
document.getElementById('kill-cancel').onclick = () => armKill(false);
document.getElementById('kill-confirm').onclick = async () => {
  armKill(false);
  await api('/v1/kill', {
    method: 'POST', body: JSON.stringify({ reason: 'engaged at the console' }),
  });
  refresh();
};
document.getElementById('release-btn').onclick = async () => {
  await api('/v1/kill/release', { method: 'POST' });
  refresh();
};

/* --- access: the policy store (7.2.4, ADR-005 D5) -----------------------
 * The matrix the owner described, rendered and editable. Same two rules
 * as everything else here: textContent only (group names and emails are
 * operator-typed, but the page that holds the kill switch takes no
 * markup from anyone), and never overwrite the operator's unsaved edits
 * — editors populate only on boot, explicit reload, and after a save.
 */

function renderPolicy(store) {
  document.getElementById('policy-version').textContent =
    store.active ? store.version : 'INACTIVE';
  document.getElementById('policy-status').textContent = store.active
    ? `version ${store.version} · ${store.servers.length} servers · ` +
      `${Object.keys(store.people).length} people · activated ${ago(store.loaded_at)}`
    : 'NO ACTIVE POLICY — the person path denies closed. Fix the store in Advanced below.';
  document.getElementById('gui-editor').classList.toggle('hidden', !store.active);
}

/* --- the GUI editor (7.2.6): forms over the same store -------------------
 * The owner's ask, verbatim: "an actual GUI... Groups, permissions,
 * tools" with the raw layer kept underneath in Advanced. All edits go
 * into a client-side DRAFT; Save serializes the draft through the SAME
 * validate→activate gate as a raw save. The slow poll never rebuilds
 * these forms — unsaved edits are the operator's.
 */

// Canonical values stay on the wire; humans read consequences.
const LEVEL_LABEL = {
  none: 'no access',
  read: 'read',
  'write-on-request': 'write — self-approve, timed',
  'write-on-approval': 'write — needs approval',
  write: 'write — always',
};
// Dropdown order = the owner's ladder; RANK order = permissiveness,
// for resolving "whichever is higher" across a person's groups.
const LEVEL_DROPDOWN = ['none', 'read', 'write-on-request',
                        'write-on-approval', 'write'];
const LEVEL_RANK = ['none', 'read', 'write-on-approval',
                    'write-on-request', 'write'];
const rank = (l) => LEVEL_RANK.indexOf(l || 'none');
const PEOPLE_CAP = 20;

let draft = null;
let draftDirty = false;
let activeTab = 'groups';
let expandedGroup = null;
let expandedPerson = null;
let peopleQuery = '';

function setGuiState(msg) {
  document.getElementById('gui-save-state').textContent = msg;
}
function markDirty() {
  draftDirty = true;
  setGuiState('unsaved changes — Save & activate to apply');
}
function initDraft(store) {
  draft = structuredClone({
    groups: store.groups, people: store.people,
    matrix: store.matrix, servers: store.servers_detail,
  });
  draft.matrix.defaults ||= {};
  draft.matrix.defaults.windows ||= [30, 60, 120];
  draft.matrix.grants ||= {};
  draft.matrix.forbids ||= [];
  draftDirty = false;
  setGuiState('');
}

const opt = (value, label) => {
  const o = el('option', null, label ?? value);
  o.value = value;
  return o;
};
function mkSelect(values, current, onchange) {
  const s = el('select');
  for (const v of values) s.append(opt(v));
  s.value = current;
  s.onchange = () => { onchange(s.value); markDirty(); };
  return s;
}
function mkList(values, onchange, size = 44) {
  const i = document.createElement('input');
  i.size = size;
  i.value = (values || []).join(', ');
  i.onchange = () => {
    onchange(i.value.split(',').map((s) => s.trim()).filter(Boolean));
    markDirty();
  };
  return i;
}
const mkX = (fn) => {
  const b = el('button', 'ghost x', '✕');
  b.onclick = () => { fn(); markDirty(); buildEditor(); };
  return b;
};

/* Resolution helpers — the GUI answers questions with EFFECTIVE access
 * ("whichever is higher", with provenance), the way the policy itself
 * resolves: permits are additive across a person's group closure, and
 * hard limits (forbids) trump everything. */

function groupClosure(g) {
  const out = new Set();
  let cur = g;
  while (cur && !out.has(cur) && draft.groups[cur]) {
    out.add(cur);
    cur = draft.groups[cur].parent;
  }
  return out;
}
function personClosure(email) {
  const out = groupClosure('all-employees');
  for (const g of ((draft.people[email] || {}).groups || [])) {
    for (const x of groupClosure(g)) out.add(x);
  }
  return out;
}
const cellLevel = (g, s) =>
  ((draft.matrix.grants[g] || {})[s] || {}).level || 'none';
function effective(closure, s) {
  let best = { level: 'none', via: null };
  for (const g of closure) {
    const l = cellLevel(g, s);
    if (rank(l) > rank(best.level)) best = { level: l, via: g };
  }
  return best;
}
const forbidsOn = (s, closure) =>
  (draft.matrix.forbids || []).filter((r) =>
    r.server === s && (!r.group || !closure || closure.has(r.group)));
const membersOf = (g) =>
  Object.keys(draft.people).filter((e) => personClosure(e).has(g)).sort();

const chip = (text, cls) => el('span', `chip${cls ? ' ' + cls : ''}`, text);
function levelChip(level, via) {
  const c = chip(LEVEL_LABEL[level] || level, `lv-${level}`);
  if (via && via !== 'all-employees') c.append(el('span', 'via', ` via ${via}`));
  else if (via) c.append(el('span', 'via', ' everyone'));
  return c;
}
const neverChip = (r) => chip(
  `never ${(r.actions || ['write'])[0]}${r.tier ? ' on ' + r.tier : ''}`, 'never');
function chipList(box, items, cap = 10) {
  items.slice(0, cap).forEach((t) => box.append(chip(t)));
  if (items.length > cap) box.append(chip(`+${items.length - cap} more`, 'muted'));
  if (!items.length) box.append(el('span', 'ctx', 'nobody'));
}
function mkLevelSelect(current, onchange) {
  const s = el('select');
  for (const v of LEVEL_DROPDOWN) s.append(opt(v, LEVEL_LABEL[v]));
  s.value = current;
  s.onchange = () => { onchange(s.value); markDirty(); buildEditor(); };
  return s;
}
function setLevel(g, s, v) {
  if (v === 'none') {
    if (draft.matrix.grants[g]) {
      delete draft.matrix.grants[g][s];
      if (!Object.keys(draft.matrix.grants[g]).length) {
        delete draft.matrix.grants[g];
      }
    }
  } else {
    (draft.matrix.grants[g] ||= {})[s] =
      { ...((draft.matrix.grants[g] || {})[s] || {}), level: v };
  }
}
function adderRow(pane, inputs, label, fn) {
  const row = el('div', 'acts adder');
  const els = inputs.map(([ph, size]) => {
    const i = document.createElement('input');
    i.placeholder = ph; i.size = size; i.autocomplete = 'off';
    return i;
  });
  const b = el('button', 'ghost', label);
  b.onclick = () => { fn(...els.map((i) => i.value.trim())); };
  row.append(...els, b);
  pane.append(row);
}

/* --- lens 1: Groups — click one, see members + what it grants ------------ */

function buildGroupsPane() {
  const pane = document.getElementById('tab-groups');
  pane.replaceChildren();
  const groups = Object.keys(draft.groups).sort();
  const servers = Object.keys(draft.servers).sort();
  for (const g of groups) {
    const card = el('div', 'card' + (expandedGroup === g ? ' open' : ''));
    const head = el('div', 'cardhead');
    head.append(el('span', 'id', g));
    const mem = membersOf(g);
    head.append(el('span', 'ctx',
      `${mem.length} member${mem.length === 1 ? '' : 's'}`));
    if (draft.groups[g].parent) {
      head.append(el('span', 'ctx', `inherits ${draft.groups[g].parent}`));
    }
    head.onclick = () => {
      expandedGroup = expandedGroup === g ? null : g;
      buildEditor();
    };
    card.append(head);

    if (expandedGroup === g) {
      const body = el('div', 'cardbody');
      const mrow = el('div', 'prow');
      mrow.append(el('span', 'ctx', 'members:'));
      const mbox = el('span', 'chips');
      chipList(mbox, mem);
      mrow.append(mbox);
      body.append(mrow);

      if (g === 'all-employees') {
        body.append(el('div', 'ctx', 'the birthright base — every person, always'));
      } else {
        const prow = el('div', 'prow');
        prow.append(el('span', 'ctx', 'parent group:'));
        prow.append(mkSelect(['—', ...groups.filter((x) => x !== g)],
          draft.groups[g].parent || '—', (v) => {
            draft.groups[g].parent = v === '—' ? null : v;
            buildEditor();
          }));
        prow.append(mkX(() => {
          delete draft.groups[g];
          if (expandedGroup === g) expandedGroup = null;
        }));
        body.append(prow);
      }

      body.append(el('div', 'ctx sep', 'access this group grants:'));
      const closure = groupClosure(g);
      for (const s of servers) {
        const row = el('div', 'prow');
        row.append(el('span', 'id', s));
        row.append(mkLevelSelect(cellLevel(g, s), (v) => setLevel(g, s, v)));
        const eff = effective(closure, s);
        if (eff.via && eff.via !== g && rank(eff.level) > rank(cellLevel(g, s))) {
          row.append(chip(`${LEVEL_LABEL[eff.level]} inherited via ${eff.via}`, 'muted'));
        }
        for (const r of forbidsOn(s, closure)) row.append(neverChip(r));
        body.append(row);
      }
      card.append(body);
    }
    pane.append(card);
  }
  adderRow(pane, [['new group name', 18]], 'Add group', (name) => {
    if (!name) return;
    draft.groups[name] ||= { parent: null };
    expandedGroup = name;
    markDirty();
    buildEditor();
  });
}

/* --- lens 2: People — search, capped list, effective access ------------- */

function buildPeoplePane() {
  const pane = document.getElementById('tab-people');
  pane.replaceChildren();
  const srow = el('div', 'prow');
  const search = document.createElement('input');
  search.placeholder = 'search people…';
  search.size = 28;
  search.value = peopleQuery;
  const listBox = el('div');
  search.oninput = () => {
    peopleQuery = search.value;
    buildPeopleList(listBox);
  };
  srow.append(search);
  pane.append(srow, listBox);
  buildPeopleList(listBox);
  adderRow(pane, [['email', 24], ['display name', 14]], 'Add person',
    (email, name) => {
      email = email.toLowerCase();
      if (!email) return;
      draft.people[email] = {
        ...(name ? { display_name: name } : {}), groups: [] };
      expandedPerson = email;
      markDirty();
      buildEditor();
    });
}

function buildPeopleList(listBox) {
  listBox.replaceChildren();
  const q = peopleQuery.trim().toLowerCase();
  const all = Object.keys(draft.people).sort().filter((e) =>
    !q || e.includes(q) ||
    ((draft.people[e].display_name || '').toLowerCase().includes(q)));
  const shown = all.slice(0, PEOPLE_CAP);
  if (all.length > PEOPLE_CAP) {
    listBox.append(el('div', 'ctx',
      `showing ${shown.length} of ${all.length} — search to narrow`));
  }
  const servers = Object.keys(draft.servers).sort();
  const groups = Object.keys(draft.groups).sort();
  for (const email of shown) {
    const p = draft.people[email];
    const card = el('div', 'card' + (expandedPerson === email ? ' open' : ''));
    const head = el('div', 'cardhead');
    head.append(el('span', 'id', email));
    if (p.display_name) head.append(el('span', 't', p.display_name));
    const gbox = el('span', 'chips');
    chipList(gbox, p.groups || [], 6);
    head.append(gbox);
    head.onclick = () => {
      expandedPerson = expandedPerson === email ? null : email;
      buildEditor();
    };
    card.append(head);

    if (expandedPerson === email) {
      const body = el('div', 'cardbody');
      const grow = el('div', 'prow');
      grow.append(el('span', 'ctx', 'groups:'));
      for (const g of (p.groups || [])) {
        const c = chip(g);
        const x = el('button', 'ghost x', '✕');
        x.onclick = () => {
          p.groups = (p.groups || []).filter((y) => y !== g);
          markDirty();
          buildEditor();
        };
        c.append(x);
        grow.append(c);
      }
      const addable = groups.filter((g) =>
        g !== 'all-employees' && !(p.groups || []).includes(g));
      if (addable.length) {
        grow.append(mkSelect(['add to group…', ...addable],
          'add to group…', (v) => {
            if (v === 'add to group…') return;
            p.groups = [...(p.groups || []), v].sort();
            buildEditor();
          }));
      }
      grow.append(mkX(() => {
        delete draft.people[email];
        if (expandedPerson === email) expandedPerson = null;
      }));
      body.append(grow);

      body.append(el('div', 'ctx sep', 'can, right now (resolved):'));
      const closure = personClosure(email);
      let any = false;
      for (const s of servers) {
        const eff = effective(closure, s);
        const nevers = forbidsOn(s, closure);
        if (eff.level === 'none' && !nevers.length) continue;
        any = true;
        const row = el('div', 'prow');
        row.append(el('span', 'id', s));
        if (eff.level !== 'none') row.append(levelChip(eff.level, eff.via));
        nevers.forEach((r) => row.append(neverChip(r)));
        body.append(row);
      }
      if (!any) {
        body.append(el('div', 'ctx',
          'no access anywhere yet — add a group above'));
      }
      card.append(body);
    }
    listBox.append(card);
  }
  if (!shown.length) listBox.append(el('p', 'empty', 'no people match'));
}

/* --- lens 3: Servers — tools, environments, and who can reach it --------- */

function buildServersPane() {
  const pane = document.getElementById('tab-servers');
  pane.replaceChildren();
  const servers = Object.keys(draft.servers).sort();
  for (const name of servers) {
    const spec = draft.servers[name];
    const card = el('div', 'card open');
    const head = el('div', 'cardhead noclick');
    head.append(el('span', 'id', name));
    head.append(mkX(() => delete draft.servers[name]));
    card.append(head);
    const body = el('div', 'cardbody');

    const who = el('div', 'prow');
    who.append(el('span', 'ctx', 'who can reach it:'));
    const byLevel = {};
    for (const email of Object.keys(draft.people)) {
      const eff = effective(personClosure(email), name);
      if (eff.level !== 'none') (byLevel[eff.level] ||= []).push(email);
    }
    let anyone = false;
    for (const lvl of [...LEVEL_RANK].reverse()) {
      const ppl = byLevel[lvl];
      if (!ppl) continue;
      anyone = true;
      const c = chip(`${LEVEL_LABEL[lvl]}: ${ppl.slice(0, 3).join(', ')}` +
                     (ppl.length > 3 ? ` +${ppl.length - 3}` : ''), `lv-${lvl}`);
      who.append(c);
    }
    if (!anyone) who.append(el('span', 'ctx', 'nobody — assign it in Groups'));
    body.append(who);

    const r1 = el('div', 'prow');
    r1.append(el('span', 'ctx', 'read tools:'),
              mkList(spec.read, (v) => { spec.read = v; }));
    const r2 = el('div', 'prow');
    r2.append(el('span', 'ctx', 'write tools:'),
              mkList(spec.write, (v) => { spec.write = v; }));
    body.append(r1, r2);

    if (spec.resource) {
      const tiers = Object.keys((spec.resource || {}).tiers || {});
      body.append(el('div', 'ctx',
        `environments: ${tiers.join(', ') || '—'} — rules can treat these ` +
        'differently (e.g. "never write on prod"). The mapping itself is ' +
        'per-server config, editable in Advanced.'));
    } else {
      body.append(el('div', 'ctx',
        'no environments configured — every call on this server is treated ' +
        'the same (fine for simple servers).'));
    }
    card.append(body);
    pane.append(card);
  }
  adderRow(pane, [['new server name', 18]], 'Add server', (name) => {
    if (!name) return;
    draft.servers[name] ||= { read: ['rpc.*'], write: [] };
    markDirty();
    buildEditor();
  });
}

/* --- lens 4: Limits & windows -------------------------------------------- */

function buildLimitsPane() {
  const pane = document.getElementById('tab-limits');
  pane.replaceChildren();
  const servers = Object.keys(draft.servers).sort();

  pane.append(el('div', 'ctx',
    'Never allow — hard limits no window, approval, or grant can cross. ' +
    'These beat everything above them.'));
  const fbox = el('div');
  (draft.matrix.forbids || []).forEach((rule, i) => {
    const row = el('div', 'prow');
    row.append(el('span', 'ctx', 'never'));
    row.append(mkSelect(['write', 'read'], (rule.actions || ['write'])[0],
      (v) => { rule.actions = [v]; }));
    row.append(el('span', 'ctx', 'on'));
    row.append(mkSelect(servers, rule.server, (v) => {
      rule.server = v;
      delete rule.tier;   // environments belong to a server; reset on switch
      buildEditor();
    }));
    row.append(el('span', 'ctx', 'environment'));
    // Environments are DECLARED per server — offer what exists, never
    // a free-typed guess (owner review: "environment prod is typed in?").
    const tiers = Object.keys(
      ((draft.servers[rule.server] || {}).resource || {}).tiers || {});
    const envOpts = ['any', ...tiers];
    if (rule.tier && !envOpts.includes(rule.tier)) envOpts.push(rule.tier);
    row.append(mkSelect(envOpts, rule.tier || 'any', (v) => {
      if (v === 'any') delete rule.tier; else rule.tier = v;
    }));
    if (!tiers.length) {
      row.append(el('span', 'ctx',
        'this server has no environments — the limit covers everything'));
    }
    row.append(mkX(() => draft.matrix.forbids.splice(i, 1)));
    fbox.append(row);
  });
  pane.append(fbox);
  const addF = el('button', 'ghost', 'Add hard limit');
  addF.onclick = () => {
    if (!servers.length || !draft) return;
    draft.matrix.forbids.push({ server: servers[0], actions: ['write'] });
    markDirty();
    buildEditor();
  };
  pane.append(addF);

  pane.append(el('div', 'ctx sep',
    'Borrow windows — the durations offered when someone self-approves ' +
    'timed write access.'));
  const wrow = el('div', 'prow');
  (draft.matrix.defaults.windows || []).forEach((w, i) => {
    const c = chip(w >= 60 ? `${w / 60} h` : `${w} min`);
    const x = el('button', 'ghost x', '✕');
    x.onclick = () => {
      draft.matrix.defaults.windows.splice(i, 1);
      markDirty();
      buildEditor();
    };
    c.append(x);
    wrow.append(c);
  });
  const wi = document.createElement('input');
  wi.type = 'number';
  wi.min = '1';
  wi.placeholder = 'minutes';
  wi.style.width = '6rem';
  const addW = el('button', 'ghost', 'Add window');
  addW.onclick = () => {
    const n = parseInt(wi.value, 10);
    if (!Number.isFinite(n) || n <= 0) return;
    const ws = draft.matrix.defaults.windows;
    if (!ws.includes(n)) ws.push(n);
    ws.sort((a, b) => a - b);
    markDirty();
    buildEditor();
  };
  wrow.append(wi, addW);
  pane.append(wrow);
}

function buildEditor() {
  if (!draft) return;
  buildGroupsPane();
  buildPeoplePane();
  buildServersPane();
  buildLimitsPane();
  buildCredsPane();
}

/* --- lens 5: Connections — the credential each MCP server uses ---------
 *
 * This exists because pasting a credential should not require shell
 * access to a host. The owner asked for it twice; the second time was
 * after being handed `sudo nano`, which is exactly the friction this
 * console exists to remove.
 *
 * The secret is write-only from here: the page can install or replace
 * one, and can never read one back. What it shows instead is enough to
 * recognise WHICH credential is installed — the App id, a fingerprint
 * of the key, and how long the current short-lived token has left.
 */
async function buildCredsPane() {
  const pane = document.getElementById('tab-creds');
  if (!pane) return;
  pane.replaceChildren();

  const intro = document.createElement('p');
  intro.className = 'hint';
  intro.textContent = 'How this platform authenticates to each tool it '
    + 'fronts. Credentials live here, never in the cluster — the servers '
    + 'themselves hold nothing, so compromising one steals no key.';
  pane.appendChild(intro);

  const list = document.createElement('div');
  pane.appendChild(list);

  async function refresh() {
    list.replaceChildren();
    let rows = [];
    try {
      const r = await api('/v1/upstream-credentials');
      rows = (r && r.servers) || [];
    } catch (e) { /* shown as empty below */ }
    if (!rows.length) {
      const none = document.createElement('p');
      none.className = 'hint';
      none.textContent = 'No connections configured yet. A server without '
        + 'one is still governed by policy — it simply cannot be called.';
      list.appendChild(none);
    }
    rows.forEach((row) => {
      const line = document.createElement('div');
      line.className = 'row';
      const name = document.createElement('strong');
      name.textContent = row.server;
      const detail = document.createElement('span');
      detail.className = 'hint';
      detail.textContent = ' — ' + row.detail;
      // Registering a server should be the last manual step. This asks
      // it what it can do and writes the classification, so nobody
      // retypes verbs into YAML.
      const disc = document.createElement('button');
      disc.textContent = 'Discover tools';
      disc.className = 'grant';
      disc.onclick = async () => {
        disc.textContent = 'asking…';
        try {
          const out = await api('/v1/upstream-credentials/'
            + encodeURIComponent(row.server) + '/discover', { method: 'POST' });
          const left = (out.destructive || []).length;
          disc.textContent = `${out.read.length} read / ${out.write.length} write`
            + (left ? ` · ${left} destructive left off` : '');
          loadAccess(true);
        } catch (e) {
          disc.textContent = String((e && e.message) || e);
        }
      };
      const del = document.createElement('button');
      del.textContent = 'Remove';
      del.className = 'deny';
      del.onclick = async () => {
        await api('/v1/upstream-credentials/' + encodeURIComponent(row.server),
                  { method: 'DELETE' });
        refresh();
      };
      line.append(name, detail, disc, del);
      list.appendChild(line);
    });
  }

  const form = document.createElement('form');
  form.className = 'card';
  const h = document.createElement('h3');
  h.textContent = 'Connect a tool';
  form.appendChild(h);

  const server = document.createElement('input');
  server.placeholder = 'server name (e.g. github)';
  server.size = 20;
  const appId = document.createElement('input');
  appId.placeholder = 'GitHub App ID (numbers only)';
  appId.size = 24;
  const install = document.createElement('input');
  install.placeholder = 'installation ID (optional)';
  install.size = 24;
  // WHERE the tool lives belongs next to HOW we authenticate to it —
  // otherwise choosing GitHub's hosted server over the one this
  // platform runs would mean editing a file on a host (owner
  // feedback: the first version of this screen "doesn't even account
  // for the remote github mcp server").
  const where = document.createElement('select');
  [['', 'runs on this platform (recommended)'],
   ['https://api.githubcopilot.com/mcp/', "GitHub's hosted server"],
   ['custom', 'another address…']].forEach(([v, label]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = label;
    where.appendChild(o);
  });
  const customUrl = document.createElement('input');
  customUrl.placeholder = 'https://…';
  customUrl.size = 40;
  customUrl.style.display = 'none';
  where.onchange = () => {
    customUrl.style.display = where.value === 'custom' ? '' : 'none';
  };
  const key = document.createElement('textarea');
  key.placeholder = '-----BEGIN RSA PRIVATE KEY-----\n…paste the .pem GitHub '
    + 'gave you…';
  key.rows = 6;
  key.style.width = '100%';
  const token = document.createElement('input');
  token.placeholder = 'or a plain token (Slack xoxb-…, etc.)';
  token.size = 40;

  // Which rung of the identity ladder this connection is on
  // (ADR-005 D10). Shown as a choice rather than buried in a file,
  // because "which of our tools flatten identity" is the question a
  // reviewer will actually ask.
  const identity = document.createElement('select');
  [['shared', 'one shared identity — read-only tools only'],
   ['per-caller', 'each person acts as themselves (recommended)']]
    .forEach(([v, label]) => {
      const o = document.createElement('option');
      o.value = v; o.textContent = label;
      identity.appendChild(o);
    });
  // Per-caller needs the upstream's OAuth client so people can link
  // their own accounts.
  const clientId = document.createElement('input');
  clientId.placeholder = 'OAuth client ID (for per-person sign-in)';
  clientId.size = 32;
  const clientSecret = document.createElement('input');
  clientSecret.placeholder = 'OAuth client secret';
  clientSecret.size = 32;
  clientSecret.type = 'password';
  const identityNote = document.createElement('p');
  identityNote.className = 'hint';
  const syncIdentity = () => {
    const per = identity.value === 'per-caller';
    clientId.style.display = clientSecret.style.display = per ? '' : 'none';
    identityNote.textContent = per
      ? 'Each person links their own account once. The upstream then sees '
        + 'the human and enforces THEIR permissions — someone who cannot do '
        + 'it there cannot do it here. Required for any tool that writes.'
      : 'Everyone acts as one identity at the upstream. Acceptable only for '
        + 'read-only access that the credential itself scopes.';
  };
  identity.onchange = syncIdentity;
  syncIdentity();

  const why = document.createElement('p');
  why.className = 'hint';
  why.textContent = 'Use a GitHub App rather than a personal token: its key '
    + 'does not expire, and this console mints a fresh one-hour credential '
    + 'whenever a call needs one. Nothing to rotate by hand.';

  const save = document.createElement('button');
  save.textContent = 'Save connection';
  save.className = 'grant';
  const status = document.createElement('span');
  status.className = 'hint';

  save.onclick = async (ev) => {
    ev.preventDefault();
    status.textContent = 'saving…';
    try {
      await api('/v1/upstream-credentials/' + encodeURIComponent(server.value.trim()),
                { method: 'PUT',
                  body: JSON.stringify({
                    app_id: appId.value, installation_id: install.value,
                    private_key: key.value, token: token.value,
                    identity: identity.value, client_id: clientId.value,
                    client_secret: clientSecret.value,
                    url: where.value === 'custom' ? customUrl.value
                      : where.value }) });
      status.textContent = 'saved — it takes effect on the next call';
      // never leave a secret on screen
      key.value = ''; token.value = ''; clientSecret.value = '';
      refresh();
    } catch (e) {
      status.textContent = String((e && e.message) || e);
    }
  };

  [server, appId, install, where, customUrl, identity, identityNote,
   clientId, clientSecret, key, token, why, save, status].forEach((el) => {
    form.appendChild(el);
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT') {
      form.appendChild(document.createElement('br'));
    }
  });
  pane.appendChild(form);
  refresh();
}

function renderGuiErrors(errs) {
  const box = document.getElementById('gui-errors');
  box.replaceChildren();
  for (const e of errs || []) box.append(el('div', null, `✗ ${e}`));
}

document.getElementById('access-tabs').onclick = (ev) => {
  const b = ev.target.closest('button.tab');
  if (!b) return;
  activeTab = b.dataset.tab;
  for (const t of document.querySelectorAll('#access-tabs .tab')) {
    t.classList.toggle('on', t === b);
  }
  for (const p of document.querySelectorAll('.tabpane')) {
    p.classList.toggle('hidden', p.id !== `tab-${activeTab}`);
  }
};
document.getElementById('gui-save').onclick = async () => {
  if (!draft) return;
  setGuiState('validating…');
  renderGuiErrors([]);
  try {
    const out = await api('/v1/policy/store/structured', {
      method: 'PUT',
      body: JSON.stringify({ groups: draft.groups, people: draft.people,
                             matrix: draft.matrix, servers: draft.servers }),
    });
    setGuiState(`activated ${out.version}`);
    await loadAccess(true);
    refresh();
  } catch (e) {
    setGuiState('rejected — nothing changed');
    renderGuiErrors(e.detail || [e.message]);
  }
};
document.getElementById('gui-reset').onclick = () =>
  loadAccess(true).catch(() => {});

function populateEditors(docs) {
  for (const key of ['entities', 'matrix', 'servers', 'overlay']) {
    document.getElementById(`ta-${key}`).value = docs[key] || '';
  }
}

function renderPolicyErrors(errs) {
  const box = document.getElementById('policy-errors');
  box.replaceChildren();
  for (const e of errs || []) box.append(el('div', null, `✗ ${e}`));
}

function renderHistory(rows) {
  const box = document.getElementById('policy-history');
  box.replaceChildren();
  for (const r of rows.slice(0, 15)) {
    const row = el('div', 'row');
    row.append(el('code', 'id', r.version), el('span', 't', r.actor),
               el('span', 'tail', ago(r.ts)));
    if (r.current) {
      row.append(el('span', 'live', 'active'));
    } else {
      const b = el('button', 'ghost', 'Restore');
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api('/v1/policy/revert', {
            method: 'POST', body: JSON.stringify({ version: r.version }),
          });
          await loadAccess(true);
          refresh();
        } catch (e) {
          renderPolicyErrors(e.detail || [e.message]);
          b.disabled = false;
        }
      };
      row.append(b);
    }
    box.append(row);
  }
}

async function loadAccess(populate) {
  const [store, hist] = await Promise.all([
    api('/v1/policy/store'), api('/v1/policy/history'),
  ]);
  renderPolicy(store);
  renderHistory(hist);
  if (populate) {
    populateEditors(store.documents);
    if (store.active) {
      initDraft(store);
      buildEditor();
    }
  }
}

document.getElementById('policy-save').onclick = async () => {
  const state = document.getElementById('policy-save-state');
  state.textContent = 'validating…';
  renderPolicyErrors([]);
  try {
    const out = await api('/v1/policy/store', {
      method: 'PUT',
      body: JSON.stringify({
        entities: document.getElementById('ta-entities').value,
        matrix: document.getElementById('ta-matrix').value,
        servers: document.getElementById('ta-servers').value,
        overlay: document.getElementById('ta-overlay').value,
      }),
    });
    state.textContent = `activated ${out.version}`;
    await loadAccess(true);
    refresh();
  } catch (e) {
    state.textContent = 'rejected — nothing changed';
    renderPolicyErrors(e.detail || [e.message]);
  }
};

document.getElementById('policy-reload').onclick = () =>
  loadAccess(true).catch(() => {});

/* --- live grants: the revocation panel (7.2.1) --------------------------- */

function renderGrants(rows) {
  const box = document.getElementById('grants');
  box.replaceChildren();
  document.getElementById('grant-count').textContent = rows.length;
  document.getElementById('grants-empty').classList.toggle('hidden', rows.length > 0);
  for (const g of rows) {
    const row = el('div', 'row');
    row.append(el('span', 'id', g.profile ? `${g.profile}` : g.tool));
    row.append(el('span', 't', g.principal || (g.flow_id ? `flow ${g.flow_id}` : '—')));
    row.append(el('span', 'tail',
      `${g.granted_via} · by ${g.granted_by} · ${until(g.expires_at)}`));
    const b = el('button', 'deny', 'Revoke');
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/v1/grants/${g.grant_id}/revoke`, {
          method: 'POST',
          body: JSON.stringify({ reason: 'revoked at the console' }),
        });
      } catch (e) { /* row refreshes to truth either way */ }
      refresh();
    };
    row.append(b);
    box.append(row);
  }
}

/* --- the poll ----------------------------------------------------------- */

let accessTick = 0;

async function refresh() {
  const link = document.getElementById('link');
  try {
    const [kill, pending, flows, audit, grants] = await Promise.all([
      api('/v1/kill'),
      api('/v1/capability-requests'),
      api('/v1/flows?active=true'),
      api('/v1/audit-events?limit=50'),
      api('/v1/grants?live=true&limit=100'),
    ]);
    const byFlow = {};
    for (const e of audit) (byFlow[e.flow_id] ||= []).push(e);

    renderKill(kill);
    renderPending(pending, byFlow);
    renderFlows(flows);
    renderAudit(audit);
    renderGrants(grants);
    // Policy data changes rarely; poll it gently and NEVER touch the
    // editors from the timer (unsaved edits are the operator's).
    if (accessTick++ % 8 === 0) loadAccess(false).catch(() => {});

    link.className = 'link live';
    link.textContent = `live · ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (String(e.message).startsWith('401')) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      return boot();   // session expired — ask for the authenticator again
    }
    link.className = 'link lost';
    link.textContent = `NO CONTACT WITH SENTINEL — showing stale data (${e.message})`;
  }
}

/* --- authentication ------------------------------------------------------
 *
 * WebAuthn's browser API speaks ArrayBuffers while JSON speaks strings, so
 * every ceremony is a base64url conversion sandwich. The private key never
 * leaves the authenticator; what crosses the wire is a signature over a
 * challenge bound to THIS origin, which is why a lookalike site cannot
 * replay it — that property is the whole reason the kill switch is behind
 * a passkey and not a password.
 */

const b64uToBuf = (s) => {
  const pad = s.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(pad + '='.repeat((4 - (pad.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
};
const bufToB64u = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

const gateError = (e) => {
  // Show the DOMException NAME, not just its prose. "UnknownError" and
  // "NotAllowedError" mean completely different things, and the messages
  // browsers attach are famously unhelpful ("the operation failed for an
  // unknown transient reason"), so the name is the only actionable part.
  const el = document.getElementById('gate-error');
  if (!e) { el.textContent = ''; return; }
  el.textContent = typeof e === 'string' ? e
    : `${e.name || 'Error'}: ${e.message || e}`;
};

// The installer hands over one clickable URL with the enrollment code in
// the fragment. Fragments never reach the server, so the code stays out
// of access logs — and the operator never copies anything between
// windows.
function codeFromUrl() {
  const m = /[#&]enroll=([A-Za-z0-9_-]+)/.exec(location.hash || '');
  return m ? m[1] : null;
}

async function doRegister() {
  gateError('');
  const code = (document.getElementById('enroll-code').value.trim()
                || codeFromUrl() || '');
  if (!code) return gateError('Open the link the installer printed, or paste a code.');
  try {
    const started = await api('/auth/register/begin', {
      method: 'POST', body: JSON.stringify({ code }),
    });
    const opts = JSON.parse(started.options).publicKey || JSON.parse(started.options);
    const challenge = opts.challenge;
    opts.challenge = b64uToBuf(opts.challenge);
    opts.user.id = b64uToBuf(opts.user.id);
    (opts.excludeCredentials || []).forEach((c) => { c.id = b64uToBuf(c.id); });

    const cred = await navigator.credentials.create({ publicKey: opts });
    await api('/auth/register/complete', {
      method: 'POST',
      body: JSON.stringify({
        _challenge: challenge,
        credential: {
          id: cred.id,
          rawId: bufToB64u(cred.rawId),
          type: cred.type,
          response: {
            clientDataJSON: bufToB64u(cred.response.clientDataJSON),
            attestationObject: bufToB64u(cred.response.attestationObject),
          },
        },
      }),
    });
    await boot();
  } catch (e) {
    gateError(e.message || String(e));
  }
}

async function doPasskeyLogin() {
  gateError('');
  try {
    const started = await api('/auth/login/begin', { method: 'POST', body: '{}' });
    const opts = JSON.parse(started.options).publicKey || JSON.parse(started.options);
    const challenge = opts.challenge;
    opts.challenge = b64uToBuf(opts.challenge);
    (opts.allowCredentials || []).forEach((c) => { c.id = b64uToBuf(c.id); });

    const cred = await navigator.credentials.get({ publicKey: opts });
    await api('/auth/login/complete', {
      method: 'POST',
      body: JSON.stringify({
        _challenge: challenge,
        credential: {
          id: cred.id,
          rawId: bufToB64u(cred.rawId),
          type: cred.type,
          response: {
            clientDataJSON: bufToB64u(cred.response.clientDataJSON),
            authenticatorData: bufToB64u(cred.response.authenticatorData),
            signature: bufToB64u(cred.response.signature),
            userHandle: cred.response.userHandle
              ? bufToB64u(cred.response.userHandle) : null,
          },
        },
      }),
    });
    await boot();
  } catch (e) {
    gateError(e.message || String(e));
  }
}

async function doTotpLogin() {
  gateError('');
  try {
    await api('/auth/login/totp', {
      method: 'POST',
      body: JSON.stringify({
        username: document.getElementById('totp-user').value.trim(),
        code: document.getElementById('totp-code').value.trim(),
      }),
    });
    await boot();
  } catch (e) {
    gateError(e.message || String(e));
  }
}

document.getElementById('enroll-btn').onclick = doRegister;
document.getElementById('passkey-btn').onclick = doPasskeyLogin;
document.getElementById('totp-btn').onclick = doTotpLogin;
document.getElementById('show-totp').onclick = () =>
  document.getElementById('totp-form').classList.toggle('hidden');
document.getElementById('logout').onclick = async () => {
  await api('/auth/logout', { method: 'POST', body: '{}' });
  await boot();
};

let pollTimer = null;

async function boot() {
  const st = await api('/auth/status');
  const signedIn = st.authenticated;
  document.getElementById('gate').classList.toggle('hidden', signedIn);
  document.getElementById('app').classList.toggle('hidden', !signedIn);
  document.getElementById('logout').classList.toggle('hidden', !signedIn);
  document.getElementById('operator').textContent = st.operator || '—';
  if (!signedIn) {
    // No authenticator on the host yet → show how to make one, not a
    // sign-in prompt nobody can satisfy.
    // A link carrying a code means "enroll", even when someone is
    // already registered — that is how a second device gets added.
    const urlCode = codeFromUrl();
    const enrolling = !st.enrolled || !!urlCode;
    document.getElementById('gate-enroll').classList.toggle('hidden', !enrolling);
    document.getElementById('gate-signin').classList.toggle('hidden', enrolling);
    document.getElementById('gate-title').textContent =
      enrolling ? 'Register this device' : 'Sign in';
    if (urlCode) document.getElementById('enroll-code').value = urlCode;
    document.getElementById('link').className = 'link';
    document.getElementById('link').textContent = 'not signed in';
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    return;
  }
  gateError('');
  await refresh();
  await loadAccess(true).catch(() => {});
  if (!pollTimer) pollTimer = setInterval(refresh, POLL_MS);
}

boot();

