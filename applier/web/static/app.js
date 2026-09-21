/* applier — application shell.
 *
 * The panels are ES modules loaded on demand. There is no build step and no
 * framework: the whole UI is DOM calls, which keeps the CSP as tight as it is
 * (no 'unsafe-inline', no 'unsafe-eval') and means the thing that renders a
 * date of birth has no third-party code in it.
 *
 * One rule throughout: text goes into the DOM through textContent, never
 * innerHTML. Job titles, company names and question text all come from
 * employer pages, and an employer page is not a trusted source.
 */

/* The session token arrives in the URL fragment (#t=...) because a fragment is
 * never transmitted — it cannot reach a server log, a Referer header or a
 * proxy. It is moved into sessionStorage so a reload keeps working, and wiped
 * from the address bar immediately so it is not sitting in a screen-share.
 *
 * sessionStorage, not localStorage: the token dies with the process that
 * minted it, so persisting it past the tab would only leave a stale one. */
const TOKEN = (() => {
  const fromHash = /(?:^|[#&])t=([\w-]+)/.exec(location.hash || '');
  if (fromHash) {
    try { sessionStorage.setItem('applier.token', fromHash[1]); } catch { /* private mode */ }
    history.replaceState(null, '', location.pathname + location.search);
    return fromHash[1];
  }
  try { return sessionStorage.getItem('applier.token') || ''; } catch { return ''; }
})();

export const hasToken = () => Boolean(TOKEN);
export const token = () => TOKEN;

/* Pasting the link from the terminal into a tab that is already open changes
 * only the fragment, which does not reload the page — so the module above has
 * already run and seen nothing. Without this, the natural thing to do after a
 * server restart (paste the new link into the tab you still have open) appears
 * to do nothing at all. */
window.addEventListener('hashchange', () => {
  const m = /(?:^|[#&])t=([\w-]+)/.exec(location.hash || '');
  if (m && m[1] !== TOKEN) location.reload();
});

/* ------------------------------------------------------------------ api -- */
export async function api(path, { method = 'GET', body = null } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      'X-Applier-Token': TOKEN,
      ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : null,
    cache: 'no-store',
    credentials: 'omit' });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON: handled below */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

export const get   = (p)     => api(p);
export const post  = (p, b)  => api(p, { method: 'POST',   body: b || {} });
export const put   = (p, b)  => api(p, { method: 'PUT',    body: b || {} });
export const patch = (p, b)  => api(p, { method: 'PATCH',  body: b || {} });
export const del   = (p)     => api(p, { method: 'DELETE' });

export function artifactUrl(rel) {
  return `/api/artifact?rel=${encodeURIComponent(rel)}`;
}

/* Artifacts need the token header, and an <img src> or <iframe src> cannot
 * send one. So the bytes are fetched here and handed back as a blob: URL,
 * which those tags accept and the CSP allows. The caller owns the URL and
 * must revokeObjectURL it when done, or a session spent flicking through
 * screenshots leaks every one of them. */
export async function artifactBlobUrl(rel) {
  const res = await fetch(artifactUrl(rel), {
    headers: { 'X-Applier-Token': TOKEN },
    cache: 'no-store',
    credentials: 'omit' });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).error || msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return URL.createObjectURL(await res.blob());
}

/* Same fetch, but for a file we want to display as text rather than frame. */
export async function artifactText(rel) {
  const res = await fetch(artifactUrl(rel), {
    headers: { 'X-Applier-Token': TOKEN },
    cache: 'no-store',
    credentials: 'omit' });
  if (!res.ok) throw new Error(`could not read ${rel}`);
  return res.text();
}

/* --------------------------------------------------------------- helpers -- */
/** Build an element. Children are appended as text unless they are Nodes. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') throw new Error('innerHTML is not allowed in this UI');
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

/* Every URL rendered as a link came out of an ATS feed or an employer's page,
 * so none of them is trusted. The CSP already refuses to run a `javascript:`
 * href under script-src 'self', but that is one layer, and this origin holds
 * the session token — so the scheme is checked here too. Anything that is not
 * plain http(s) renders as inert text instead of a link. */
export function safeUrl(u) {
  if (!u) return null;
  try {
    const parsed = new URL(String(u), location.origin);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : null;
  } catch { return null; }
}

/** A link, or plain text when the URL is missing or not http(s). */
export function link(url, label, attrs = {}) {
  const safe = safeUrl(url);
  if (!safe) return el('span', { class: 'dim' }, label);
  return el('a', { ...attrs, href: safe, target: '_blank', rel: 'noreferrer noopener' }, label);
}

/* Dates reach us in four shapes, because four different sources write them:
 * SQLite 'YYYY-MM-DD HH:MM:SS', a bare 'YYYY-MM-DD', an epoch in seconds from
 * the ATS feeds, and an epoch in milliseconds from the task runner. Parsing
 * only the first two is what put a raw 1770966668 on the job detail page. */
function toDate(v) {
  if (v === null || v === undefined || v === '') return null;
  if (v instanceof Date) return isNaN(v) ? null : v;
  if (typeof v === 'number' || /^\d{9,14}$/.test(String(v).trim())) {
    const n = Number(v);
    // Seconds and milliseconds are told apart by magnitude: anything below
    // ~1e11 as milliseconds would be 1973, which no job posting is.
    const d = new Date(n < 1e11 ? n * 1000 : n);
    return isNaN(d) ? null : d;
  }
  const s = String(v).trim();
  const d = new Date(s.length <= 10 ? `${s}T00:00:00` : s.replace(' ', 'T'));
  return isNaN(d) ? null : d;
}

export function fmtDate(v) {
  const d = toDate(v);
  if (!d) return v ? String(v) : '—';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: '2-digit' });
}

export function fmtAgo(v) {
  const d = toDate(v);
  if (!d) return v ? String(v) : '—';
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

export function fmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

export function scoreCell(v) {
  if (v === null || v === undefined) return el('span', { class: 'dim' }, '—');
  const cls = v >= 0.75 ? 'hi' : v >= 0.6 ? 'mid' : 'lo';
  return el('span', { class: `score ${cls}` }, v.toFixed(2));
}

const PILLS = {
  submitted: 'ok', done: 'ok', pass: 'ok', offer: 'ok', interview: 'ok', replied: 'ok',
  queued: 'info', preparing: 'info', ready: 'info', running: 'info', drafted: 'info', oa: 'info',
  needs_input: 'warn', captcha: 'warn', scored: 'warn', new: 'warn', harvested: 'warn',
  failed: 'bad', rejected: 'bad', blocked: 'bad', rejection: 'bad', skipped: 'bad' };
export function pill(text, kind) {
  return el('span', { class: `pill ${kind || PILLS[text] || ''}` }, text || '—');
}

/* ---------------------------------------------------------------- toasts -- */
export function toast(message, kind = 'info', ms = 4200) {
  const host = document.getElementById('toasts');
  const node = el('div', { class: `toast ${kind}` }, message);
  host.appendChild(node);
  setTimeout(() => node.remove(), ms);
}

/* ----------------------------------------------------------------- modal -- */
export function modal(title, bodyNode, actions = []) {
  const host  = document.getElementById('modal');
  const bodyH = document.getElementById('modal-body');
  const actH  = document.getElementById('modal-actions');
  document.getElementById('modal-title').textContent = title;
  clear(bodyH).appendChild(bodyNode);
  clear(actH);
  const close = () => { host.hidden = true; document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  for (const a of actions) {
    actH.appendChild(el('button', {
      class: `btn ${a.primary ? 'btn-primary' : a.danger ? 'btn-danger' : ''}`,
      onclick: async () => { const keep = await a.onClick?.(); if (!keep) close(); } }, a.label));
  }
  actH.appendChild(el('button', { class: 'btn btn-ghost', onclick: close }, 'Close'));
  host.hidden = false;
  document.addEventListener('keydown', onKey);
  return close;
}

export function confirmDialog(title, message, confirmLabel = 'Confirm') {
  return new Promise((resolve) => {
    let done = false;
    const close = modal(title, el('p', { class: 'muted' }, message), [
      { label: confirmLabel, primary: true, onClick: () => { done = true; resolve(true); } },
    ]);
    const watch = setInterval(() => {
      if (document.getElementById('modal').hidden) {
        clearInterval(watch);
        if (!done) resolve(false);
      }
    }, 120);
    void close;
  });
}

/* ------------------------------------------------------------- task drawer -- */
let liveStream = null;
let liveTaskId = null;

export function watchTask(task, { onDone } = {}) {
  const drawer = document.getElementById('drawer');
  const log    = document.getElementById('drawer-log');
  const stopBtn = document.getElementById('drawer-stop');
  document.getElementById('drawer-title').textContent = task.name;
  clear(log);
  drawer.hidden = false;
  stopBtn.hidden = false;
  liveTaskId = task.id;

  if (liveStream) liveStream.close();

  // EventSource cannot send a custom header, and the token deliberately never
  // travels in a query string, so the stream is read as a fetch body instead.
  const controller = new AbortController();
  liveStream = { close: () => controller.abort() };

  stopBtn.onclick = async () => {
    try { await post(`/api/tasks/${task.id}/stop`); toast('Stopping after the current step…', 'info'); }
    catch (e) { toast(e.message, 'bad'); }
  };

  (async () => {
    try {
      const res = await fetch(`/api/tasks/${task.id}/stream`, {
        headers: { 'X-Applier-Token': TOKEN },
        signal: controller.signal,
        cache: 'no-store' });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const frames = buf.split('\n\n');
        buf = frames.pop();
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data: '));
          if (!line) continue;
          let payload;
          try { payload = JSON.parse(line.slice(6)); } catch { continue; }
          if (payload.line !== undefined) {
            const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
            log.appendChild(document.createTextNode(payload.line + '\n'));
            if (atBottom) log.scrollTop = log.scrollHeight;
          }
          if (payload.done) {
            stopBtn.hidden = true;
            const t = payload.task;
            const kind = t.status === 'done' ? 'ok' : t.status === 'stopped' ? 'info' : 'bad';
            log.appendChild(el('div', { class: 'muted' },
              `\n— ${t.status} in ${t.elapsed}s${t.result ? ` — ${t.result}` : ''}${t.error ? ` — ${t.error}` : ''}`));
            log.scrollTop = log.scrollHeight;
            toast(`${t.name}: ${t.status}`, kind, 6000);
            refreshChrome();
            onDone?.(t);
          }
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') log.appendChild(document.createTextNode(`\n[stream lost: ${e.message}]\n`));
    }
  })();
}

/** Start a task endpoint and immediately open the drawer on it. */
export async function runTask(path, body, opts) {
  const { task } = await post(path, body || {});
  watchTask(task, opts);
  refreshChrome();
  return task;
}

/* ------------------------------------------------------------------ router -- */
const PANELS = [
  { id: 'dashboard',    label: 'Dashboard',    icon: '◆', file: './panels/dashboard.js' },
  { id: 'jobs',         label: 'Jobs',         icon: '▤', file: './panels/jobs.js',        countKey: 'queued' },
  { id: 'apply',        label: 'Apply',        icon: '▸', file: './panels/apply.js' },
  { id: 'applications', label: 'Applications', icon: '✓', file: './panels/applications.js' },
  { id: 'asks',         label: 'Questions',    icon: '?', file: './panels/asks.js',        countKey: 'open_asks', alert: true },
  { id: 'resume',       label: 'Resume',       icon: '▦', file: './panels/resume.js' },
  { id: 'outreach',     label: 'Outreach',     icon: '✉', file: './panels/outreach.js',    countKey: 'drafted' },
  { id: 'settings',     label: 'Settings',     icon: '⚙', file: './panels/settings.js' },
];

const loaded = new Map();
let currentPanel = null;
let lastState = {};

async function navigate(id, params = {}) {
  const spec = PANELS.find((p) => p.id === id) || PANELS[0];
  if (!loaded.has(spec.id)) loaded.set(spec.id, await import(spec.file));
  const mod = loaded.get(spec.id);

  currentPanel = spec.id;
  document.getElementById('page-title').textContent = spec.label;
  clear(document.getElementById('topbar-actions'));
  const view = clear(document.getElementById('view'));

  for (const btn of document.querySelectorAll('#nav button')) {
    btn.classList.toggle('active', btn.dataset.id === spec.id);
  }
  const hash = `#${spec.id}${params.id ? `/${params.id}` : ''}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);

  const ctx = {
    view,
    actions: document.getElementById('topbar-actions'),
    params,
    go: navigate,
    refresh: () => navigate(currentPanel, params),
    state: lastState };
  try {
    await mod.render(ctx);
  } catch (e) {
    view.appendChild(el('div', { class: 'banner bad' }, `Could not load ${spec.label}: ${e.message}`));
    console.error(e);
  }
}
export const go = navigate;

function buildNav() {
  const nav = clear(document.getElementById('nav'));
  for (const p of PANELS) {
    const btn = el('button', { dataset: { id: p.id }, onclick: () => navigate(p.id) },
      el('span', { class: 'ico' }, p.icon),
      el('span', {}, p.label),
    );
    nav.appendChild(el('li', {}, btn));
  }
}

/* Sidebar badges + the running-task line. Cheap, so it polls. */
export async function refreshChrome() {
  try {
    lastState = await get('/api/state');
  } catch { return; }

  for (const p of PANELS) {
    const btn = document.querySelector(`#nav button[data-id="${p.id}"]`);
    if (!btn) continue;
    btn.querySelector('.count')?.remove();
    if (!p.countKey) continue;
    const n = p.countKey === 'queued' ? lastState.funnel?.queued : lastState[p.countKey];
    if (n) btn.appendChild(el('span', { class: `count ${p.alert ? 'alert' : ''}` }, String(n)));
  }

  const lvl = lastState.autonomy?.levels?.find((l) => l.key === lastState.autonomy.level);
  clear(document.getElementById('autonomy-mini')).append(
    document.createTextNode('Mode: '), el('b', {}, lvl ? lvl.label : '—'));

  const running = (lastState.tasks || []).filter((t) => t.status === 'running');
  const mini = clear(document.getElementById('running-mini'));
  if (running.length) {
    mini.appendChild(el('a', { onclick: () => watchTask(running[0]) },
      `● ${running.length} running`));
  }
}

/* ------------------------------------------------------------------- boot -- */
function wireChrome() {
  document.getElementById('drawer-close').onclick = () => {
    document.getElementById('drawer').hidden = true;
    liveStream?.close();
    liveStream = null;
    liveTaskId = null;
  };
  document.getElementById('modal').addEventListener('click', (e) => {
    if (e.target.id === 'modal') document.getElementById('modal').hidden = true;
  });
  window.addEventListener('hashchange', () => {
    const [id, sub] = location.hash.slice(1).split('/');
    if (id && id !== currentPanel) navigate(id, sub ? { id: sub } : {});
  });
}

async function boot() {
  const bootEl = document.getElementById('boot');
  if (!TOKEN) {
    clear(bootEl).append(
      el('div', { class: 'u-c1-2' },
        el('p', {}, 'No session token.'),
        el('p', { class: 'small' },
          'Open the link that "applier gui" printed in your terminal — the token ' +
          'is in the part after the #, and it is not stored anywhere on disk.')));
    return;
  }
  try {
    await get('/api/state');
  } catch (e) {
    clear(bootEl).append(
      el('div', { class: 'u-c1-2' },
        el('p', {}, `Could not reach the applier server: ${e.message}`),
        el('p', { class: 'small' },
          'If the server was restarted the token changed. Open the new link ' +
          'from the terminal.')));
    return;
  }
  document.getElementById('boot').hidden = true;
  document.getElementById('shell').hidden = false;

  buildNav();
  wireChrome();
  await refreshChrome();

  const [id, sub] = location.hash.slice(1).split('/');
  await navigate(PANELS.some((p) => p.id === id) ? id : 'dashboard', sub ? { id: sub } : {});

  setInterval(refreshChrome, 10000);
}

boot();
