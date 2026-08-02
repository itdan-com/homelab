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
    : 'NO ACTIVE POLICY — the person path denies closed until a store activates.';

  const mv = document.getElementById('matrix-view');
  mv.replaceChildren();
  if (!store.active) return;

  const grants = store.matrix.grants || {};
  const table = el('table');
  const head = el('tr');
  head.append(el('th', null, 'group \\ server'));
  for (const s of store.servers) head.append(el('th', null, s));
  table.append(head);
  for (const g of Object.keys(grants).sort()) {
    const tr = el('tr');
    tr.append(el('th', null, g));
    for (const s of store.servers) {
      const level = ((grants[g] || {})[s] || {}).level || '—';
      tr.append(el('td', level === '—' ? 'lv-none' : `lv-${level}`, level));
    }
    table.append(tr);
  }
  mv.append(table);

  const pv = document.getElementById('people-view');
  pv.replaceChildren();
  for (const email of Object.keys(store.people).sort()) {
    const row = el('div', 'row');
    row.append(el('span', 'id', email));
    row.append(el('span', 'tail',
      (store.people[email].groups || []).join(', ') || 'all-employees only'));
    pv.append(row);
  }
}

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
  if (populate) populateEditors(store.documents);
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

