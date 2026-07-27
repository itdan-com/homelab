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
    const [health, kill, pending, flows, audit] = await Promise.all([
      api('/healthz'),
      api('/v1/kill'),
      api('/v1/capability-requests'),
      api('/v1/flows?active=true'),
      api('/v1/audit-events?limit=50'),
    ]);
    document.getElementById('operator').textContent = health.operator;

    const byFlow = {};
    for (const e of audit) (byFlow[e.flow_id] ||= []).push(e);

    renderKill(kill);
    renderPending(pending, byFlow);
    renderFlows(flows);
    renderAudit(audit);

    link.className = 'link live';
    link.textContent = `live · ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    link.className = 'link lost';
    link.textContent = `NO CONTACT WITH SENTINEL — showing stale data (${e.message})`;
  }
}

refresh();
setInterval(refresh, POLL_MS);
