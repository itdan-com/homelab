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
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
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

/* --- the poll ----------------------------------------------------------- */

async function refresh() {
  const link = document.getElementById('link');
  try {
    const [kill, pending, flows, audit] = await Promise.all([
      api('/v1/kill'),
      api('/v1/capability-requests'),
      api('/v1/flows?active=true'),
      api('/v1/audit-events?limit=50'),
    ]);
    const byFlow = {};
    for (const e of audit) (byFlow[e.flow_id] ||= []).push(e);

    renderKill(kill);
    renderPending(pending, byFlow);
    renderFlows(flows);
    renderAudit(audit);

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
  if (!pollTimer) pollTimer = setInterval(refresh, POLL_MS);
}

boot();

