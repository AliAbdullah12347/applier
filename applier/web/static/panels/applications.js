/* Applications — what an employer actually received.
 *
 * The list is a ledger. The detail page is evidence: if a recruiter writes back
 * in March about something filed in September, this screen has to reconstruct
 * the exact bundle — the resume hash, every field submitted, the screenshot of
 * each step — without anyone having to remember it.
 */

import {
  el, clear, get, post, artifactUrl, token, pill, fmtDate, fmtAgo, fmtBytes, toast,
  go, link } from '../app.js';

const OUTCOMES = [
  ['acknowledged', 'Acknowledged'],
  ['oa', 'Assessment'],
  ['interview', 'Interview'],
  ['rejection', 'Rejection'],
  ['offer', 'Offer'],
  ['ghosted', 'Ghosted'],
];

const IMAGES  = new Set(['.png', '.jpg', '.jpeg']);
/* .html is absent on purpose: the artifact endpoint refuses that suffix.
 * A saved employer page is untrusted markup, and this origin holds the
 * session token. */
const TEXTUAL = new Set(['.txt', '.tex', '.md', '.json', '.log']);
const TEXT_CAP = 200000;   // a stray .log should not freeze the page

/* The status filter lives in module state, not in the URL: the shell routes an
 * id and nothing else, and losing the filter on every refresh would make
 * recording six outcomes in a row tedious. */
let listStatus = 'all';

/* ------------------------------------------------------------- artifacts -- */
/* The artifact endpoint demands the token header, which an <img src> or an
 * <iframe src> cannot send, and the shell keeps its own copy of the token
 * private. So a preview is fetched here with the header attached and handed to
 * the element as a blob: URL, which the CSP allows for both frames and images. */
const liveBlobs = new Set();

async function artifactFetch(rel) {
  const res = await fetch(artifactUrl(rel), {
    headers: { 'X-Applier-Token': token() },
    cache: 'no-store',
    credentials: 'omit' });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); if (j && j.error) msg = j.error; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return res;
}

async function artifactBlob(rel) {
  const url = URL.createObjectURL(await (await artifactFetch(rel)).blob());
  liveBlobs.add(url);
  return url;
}

function revokeBlobs() {
  for (const u of liveBlobs) URL.revokeObjectURL(u);
  liveBlobs.clear();
}

/* Nothing signals a panel unmount, so the object URLs are released when the
 * shell empties the view container. Otherwise every visit would strand a few
 * megabytes of decoded PNG in memory for the life of the page. */
function revokeWhenDetached(root) {
  if (!root.parentNode) return;
  const obs = new MutationObserver(() => {
    if (root.isConnected) return;
    obs.disconnect();
    revokeBlobs();
  });
  obs.observe(root.parentNode, { childList: true });
}

/* ------------------------------------------------------------------ entry -- */
export async function render(ctx) {
  revokeBlobs();
  if (ctx.params.id) return detailView(ctx, ctx.params.id);
  return listView(ctx);
}

/* ------------------------------------------------------------------- list -- */
async function listView(ctx) {
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => ctx.refresh() }, 'Refresh'),
    el('button', { class: 'btn btn-primary', onclick: () => go('apply') }, 'Apply to a link'),
  );

  ctx.view.appendChild(el('div', { class: 'empty' }, 'Loading applications…'));

  let data;
  try {
    const qs = listStatus && listStatus !== 'all'
      ? `?status=${encodeURIComponent(listStatus)}` : '';
    data = await get(`/api/applications${qs}`);
  } catch (e) {
    clear(ctx.view).appendChild(el('div', { class: 'banner bad' },
      `Could not load applications: ${e.message}`));
    return;
  }
  clear(ctx.view);

  const rows   = data.applications || [];
  const counts = data.counts || {};
  const total  = Object.values(counts).reduce((n, c) => n + (c || 0), 0);

  /* The counts come from the whole table, not the filtered query, so the strip
   * keeps showing where everything is even while one status is selected. */
  if (total) {
    const strip = el('div', { class: 'tabs' });
    const choose = (key) => { listStatus = key; ctx.refresh(); };
    strip.appendChild(el('button', {
      class: listStatus === 'all' ? 'on' : '',
      onclick: () => choose('all') }, `All (${total})`));
    for (const [k, c] of Object.entries(counts).sort((a, b) => a[0].localeCompare(b[0]))) {
      strip.appendChild(el('button', {
        class: listStatus === k ? 'on' : '',
        onclick: () => choose(k) }, `${k.replace(/_/g, ' ')} (${c})`));
    }
    ctx.view.appendChild(strip);
  }

  if (!rows.length) {
    ctx.view.appendChild(total
      ? el('div', { class: 'empty' },
        el('div', {}, `Nothing with the status "${listStatus}".`),
        el('div', { class: 'u-mt-10px' },
          el('button', { class: 'btn btn-sm', onclick: () => { listStatus = 'all'; ctx.refresh(); } },
            'Show all')))
      : el('div', { class: 'empty' }, 'Nothing yet. Use Apply to send your first one.'));
    return;
  }

  const tb = el('tbody');
  for (const a of rows) {
    const outcomeCell = el('td', { class: 'shrink' });
    outcomeCell.appendChild(outcomeSelect(a));
    tb.appendChild(el('tr', { class: 'clickable', onclick: () => go('applications', { id: a.id }) },
      el('td', {},
        el('div', {}, a.company || '—'),
        el('div', { class: 'dim small' }, a.title || '—'),
        a.failure_reason && a.status === 'failed'
          ? el('div', { class: 'dim small mono truncate u-maxw-420px' }, a.failure_reason)
          : null),
      el('td', { class: 'shrink' }, pill(a.status)),
      el('td', { class: 'shrink dim small' }, fmtDate(a.started_at)),
      el('td', { class: 'shrink dim small' }, a.submitted_at ? fmtDate(a.submitted_at) : '—'),
      outcomeCell,
      el('td', { class: 'shrink dim small mono' }, bundleLabel(a)),
    ));
  }

  ctx.view.appendChild(el('div', { class: 'card' },
    el('h2', {}, 'Applications',
      el('span', { class: 'sub' }, `${rows.length} shown, newest first`)),
    el('div', { class: 'scroll-y' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Role'),
          el('th', { class: 'shrink' }, 'Status'),
          el('th', { class: 'shrink' }, 'Started'),
          el('th', { class: 'shrink' }, 'Submitted'),
          el('th', { class: 'shrink' }, 'Outcome'),
          el('th', { class: 'shrink' }, 'Artifacts'))),
        tb))));
}

/* The list endpoint returns the application row only — the file listing is
 * built on demand by the detail endpoint — so a row can say a bundle exists and
 * how many screenshots it recorded, but not the true file count. */
function bundleLabel(a) {
  if (!a.artifact_dir) return '—';
  const n = parseJsonList(a.screenshots).length;
  return n ? `${n} shots` : 'bundle';
}

function outcomeSelect(a) {
  const sel = el('select', {
    onclick: (e) => e.stopPropagation(),     // the whole row navigates
    onchange: async (e) => {
      const value = e.target.value;
      sel.disabled = true;
      try {
        await post(`/api/applications/${a.id}/outcome`, { outcome: value });
        a.outcome = value || null;
        toast(`${a.company || 'Application'}: ${value || 'outcome cleared'}`, 'ok');
      } catch (err) {
        e.target.value = a.outcome || '';    // the server refused; do not show a lie
        toast(err.message, 'bad');
      } finally {
        sel.disabled = false;
      }
    } },
  el('option', { value: '', selected: !a.outcome }, '—'),
  ...OUTCOMES.map(([v, label]) => el('option', { value: v, selected: a.outcome === v }, label)));
  return sel;
}

/* ----------------------------------------------------------------- detail -- */
async function detailView(ctx, id) {
  ctx.actions.append(
    el('button', { class: 'btn btn-ghost', onclick: () => go('applications') }, 'All applications'),
    el('button', { class: 'btn', onclick: () => ctx.refresh() }, 'Refresh'),
  );

  ctx.view.appendChild(el('div', { class: 'empty' }, 'Loading the application bundle…'));

  let app;
  try {
    const r = await get(`/api/applications/${encodeURIComponent(id)}`);
    app = r.application;
  } catch (e) {
    clear(ctx.view).appendChild(el('div', { class: 'banner bad' },
      `Could not load application ${id}: ${e.message}`));
    return;
  }
  clear(ctx.view);

  const root = el('div', {});
  ctx.view.appendChild(root);
  revokeWhenDetached(root);

  /* ------------------------------------------------------------- blockers */
  if (app.status === 'needs_input' || app.status === 'captcha') {
    root.appendChild(el('div', { class: 'banner warn u-mb-14px' },
      el('span', {}, app.status === 'captcha'
        ? 'Stopped at a CAPTCHA. The browser was left open for you to clear it by hand — nothing further happens here until you do.'
        : 'Paused on a question it would not answer on your behalf. Answer it once and it is reused everywhere after.'),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm', onclick: () => go('asks') }, 'Open Questions')));
  }
  if (app.status === 'failed' && app.failure_reason) {
    root.appendChild(el('div', { class: 'banner bad u-mb-14px' },
      el('span', { class: 'small' }, `This run failed: ${app.failure_reason}`)));
  }

  /* --------------------------------------------------------------- header */
  const head = el('div', { class: 'card u-mb-16px' },
    el('div', { class: 'row' },
      el('div', {},
        el('div', { class: 'u-c2-2' }, app.company || '—'),
        el('div', { class: 'muted' }, app.title || '—'),
        el('div', { class: 'dim small' },
          [app.location, app.score != null ? `score ${Number(app.score).toFixed(2)}` : null]
            .filter(Boolean).join(' · ') || ' ')),
      el('span', { class: 'spacer' }),
      pill(app.status),
      // Both URLs come from an ATS feed, so they go through the shell's
      // scheme check instead of straight into an href.
      app.url ? link(app.url, 'Open posting', { class: 'btn btn-sm' }) : null,
      app.apply_url && app.apply_url !== app.url
        ? link(app.apply_url, 'Open form', { class: 'btn btn-sm' })
        : null));

  const kv = el('dl', { class: 'kv' });
  const kvAdd = (k, ...v) => kv.append(el('dt', {}, k), el('dd', {}, ...v));
  kvAdd('Started', app.started_at
    ? `${fmtDate(app.started_at)} · ${fmtAgo(app.started_at)}`
    : el('span', { class: 'dim' }, '—'));
  kvAdd('Submitted', app.submitted_at
    ? `${fmtDate(app.submitted_at)} · ${fmtAgo(app.submitted_at)}`
    : el('span', { class: 'dim' }, 'not submitted'));
  kvAdd('Resume file', app.resume_path
    ? el('span', { class: 'mono small' }, baseName(app.resume_path))
    : el('span', { class: 'dim' }, 'none recorded'));
  /* The hash is the only thing that proves which build of the resume went out;
   * it is shown in full rather than truncated for exactly that reason. */
  kvAdd('Resume SHA-256', app.resume_sha256
    ? el('span', { class: 'mono small' }, app.resume_sha256)
    : el('span', { class: 'dim' }, 'no hash recorded'));
  if (app.cover_path) {
    kvAdd('Cover letter', el('span', { class: 'mono small' }, baseName(app.cover_path)));
  }
  if (app.outcome_at) kvAdd('Outcome recorded', fmtDate(app.outcome_at));
  if (app.failure_reason) {
    kvAdd('Failure reason', el('span', { class: 'mono small' }, app.failure_reason));
  }
  if (app.artifact_dir) {
    kvAdd('Artifact folder', el('span', { class: 'mono small dim' }, app.artifact_dir));
  }
  head.appendChild(kv);

  /* -------------------------------------------------------------- outcome */
  const outcomeRow = el('div', { class: 'row tight u-mt-14px' });
  function paintOutcome() {
    clear(outcomeRow);
    outcomeRow.appendChild(el('span', { class: 'dim small' }, 'Outcome'));
    for (const [v, label] of OUTCOMES) {
      outcomeRow.appendChild(el('button', {
        class: `btn btn-sm ${app.outcome === v ? 'btn-primary' : ''}`,
        onclick: () => saveOutcome(app.outcome === v ? '' : v) }, label));
    }
    outcomeRow.append(
      el('span', { class: 'spacer' }),
      el('button', {
        class: 'btn btn-sm btn-ghost',
        disabled: !app.outcome,
        onclick: () => saveOutcome('') }, 'Clear'));
  }
  async function saveOutcome(value) {
    try {
      await post(`/api/applications/${app.id}/outcome`, { outcome: value });
      app.outcome = value || null;
      /* Only the buttons are repainted. The server stamps outcome_at and this
       * page will not invent a timestamp it has not been told; it appears on
       * the next load. */
      paintOutcome();
      toast(value ? `Recorded: ${value}` : 'Outcome cleared', 'ok');
    } catch (e) {
      toast(e.message, 'bad');
    }
  }
  paintOutcome();
  head.appendChild(outcomeRow);
  root.appendChild(head);

  /* ----------------------------------------------------------------- tabs */
  const answers = answerRows(app.answers);
  const shots   = screenshotRefs(app);
  const files   = app.artifacts || [];
  const events  = app.events || [];

  const tabBar = el('div', { class: 'tabs' });
  const pane   = el('div', {});
  const TABS = [
    ['Documents',   files.length,   () => documentsPane(app, files)],
    ['Answers',     answers.length, () => answersPane(answers)],
    ['Screenshots', shots.length,   () => screenshotsPane(shots)],
    ['Timeline',    events.length,  () => timelinePane(events)],
  ];
  let active = TABS[0][0];

  function paintTabs() {
    clear(tabBar);
    for (const [label, count] of TABS) {
      tabBar.appendChild(el('button', {
        class: active === label ? 'on' : '',
        onclick: () => {
          if (active === label) return;
          active = label;
          revokeBlobs();      // whatever the old tab loaded is off screen now
          paintTabs();
        } }, count ? `${label} (${count})` : label));
    }
    clear(pane).appendChild(TABS.find((t) => t[0] === active)[2]());
  }
  paintTabs();
  root.append(tabBar, pane);
}

/* -------------------------------------------------------------- documents -- */
function documentsPane(app, files) {
  if (!files.length) {
    return el('div', { class: 'empty' },
      'No files were kept for this one. A run writes the resume, the page dumps and a screenshot of every step into its artifact folder as it goes.');
  }

  const sent   = baseName(app.resume_path);
  const cover  = baseName(app.cover_path);
  const viewer = el('div', { class: 'u-mt-14px' });
  const tb = el('tbody');

  for (const f of files) {
    tb.appendChild(el('tr', {},
      el('td', {},
        el('span', { class: 'mono small' }, f.name), ' ',
        f.name === sent ? pill('sent as resume', 'ok') : null,
        f.name === cover ? pill('cover letter', 'info') : null),
      el('td', { class: 'shrink dim small mono' }, f.suffix),
      el('td', { class: 'num shrink dim small' }, fmtBytes(f.size)),
      el('td', { class: 'shrink right' },
        el('button', { class: 'btn btn-sm', onclick: () => showFile(viewer, f) }, 'View')),
    ));
  }

  return el('div', {},
    el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'File'),
        el('th', { class: 'shrink' }, 'Type'),
        el('th', { class: 'shrink right' }, 'Size'),
        el('th', { class: 'shrink' }, ''))),
      tb),
    viewer);
}

async function showFile(host, f) {
  clear(host).appendChild(el('div', { class: 'empty' }, `Loading ${f.name}…`));
  try {
    const title = el('div', { class: 'row u-mb-8px' },
      el('strong', { class: 'small mono' }, f.name),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm btn-ghost', onclick: () => clear(host) }, 'Close'));

    if (f.suffix === '.pdf') {
      const url = await artifactBlob(f.rel);
      clear(host).append(title, el('iframe', { class: 'pdf', src: url, title: f.name }));
      return;
    }
    if (IMAGES.has(f.suffix)) {
      const url = await artifactBlob(f.rel);
      clear(host).append(title, el('img', { class: 'shot', src: url, alt: f.name }));
      return;
    }
    /* Everything else is shown as text through textContent rather than framed.
     * A saved employer page is untrusted markup, and a blob: document inherits
     * this origin — which is the origin holding the session token. Reading the
     * bytes and printing them removes that question entirely. */
    let text = await (await artifactFetch(f.rel)).text();
    let note = null;
    if (text.length > TEXT_CAP) {
      note = el('div', { class: 'dim small' },
        `Showing the first ${fmtBytes(TEXT_CAP)} of ${fmtBytes(f.size)}.`);
      text = text.slice(0, TEXT_CAP);
    }
    clear(host).append(title, note, el('div', { class: 'jd mono' }, text));
  } catch (e) {
    clear(host).appendChild(el('div', { class: 'banner bad' },
      `Could not open ${f.name}: ${e.message}`));
  }
}

/* ---------------------------------------------------------------- answers -- */
/* The runner stores what it filled as a selector-to-value map, while richer
 * records (and anything written by an older run) arrive as a list of objects.
 * Both are normalised here rather than guessed at in the render loop. */
function answerRows(answers) {
  if (Array.isArray(answers)) {
    return answers.map((a) => (a && typeof a === 'object'
      ? {
        q: String(a.question ?? a.label ?? a.field ?? a.selector ?? a.key ?? '—'),
        a: fmtValue(a.answer ?? a.value ?? ''),
        s: String(a.source ?? a.origin ?? '') }
      : { q: '—', a: fmtValue(a), s: '' }));
  }
  if (answers && typeof answers === 'object') {
    return Object.entries(answers).map(([k, v]) => ({ q: k, a: fmtValue(v), s: '' }));
  }
  return [];
}

function fmtValue(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'boolean' || typeof v === 'number') return String(v);
  try { return JSON.stringify(v); } catch { return String(v); }
}

function answersPane(rows) {
  if (!rows.length) {
    return el('div', { class: 'empty' },
      'No fields were recorded. Either the run stopped before it filled anything, or it was a one-click apply with nothing to record.');
  }

  const tb    = el('tbody');
  const count = el('span', { class: 'dim small' });
  const box   = el('input', {
    type: 'text',
    placeholder: 'Filter fields and answers',
    oninput: () => paint(box.value) });

  function paint(raw) {
    const q = (raw || '').trim().toLowerCase();
    clear(tb);
    let shown = 0;
    for (const r of rows) {
      if (q && !`${r.q} ${r.a} ${r.s}`.toLowerCase().includes(q)) continue;
      shown += 1;
      tb.appendChild(el('tr', {},
        el('td', { class: 'mono small' }, r.q),
        el('td', {}, r.a || el('span', { class: 'dim' }, '(blank)')),
        el('td', { class: 'shrink dim small' }, r.s || '—')));
    }
    count.textContent = `${shown} of ${rows.length} fields`;
    if (!shown) {
      tb.appendChild(el('tr', {},
        el('td', { colspan: '3' }, el('div', { class: 'empty' }, 'Nothing matches that.'))));
    }
  }
  paint('');

  return el('div', {},
    el('div', { class: 'filters u-mb-10px' }, box, count),
    el('div', { class: 'scroll-y' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Field'),
          el('th', {}, 'Answer submitted'),
          el('th', { class: 'shrink' }, 'Source'))),
        tb)));
}

/* ------------------------------------------------------------ screenshots -- */
/* The runner records absolute paths, which the artifact endpoint will not
 * serve — it only accepts a path relative to the artifacts root. The file
 * listing already carries those, so the two are matched by name, in the order
 * the steps happened. */
function screenshotRefs(app) {
  const byName = new Map((app.artifacts || []).map((f) => [f.name, f.rel]));
  const out = [];
  const seen = new Set();
  for (const p of (app.screenshots || [])) {
    const name = String(p).split(/[\\/]/).pop();
    const rel = byName.get(name);
    if (!rel || seen.has(rel)) continue;
    seen.add(rel);
    out.push({ name, rel });
  }
  /* If nothing matched — a moved artifacts root, say — fall back to every image
   * in the bundle so the tab is never empty while the files plainly exist. The
   * order is then alphabetical rather than chronological. */
  if (!out.length) {
    for (const f of (app.artifacts || [])) {
      if (IMAGES.has(f.suffix)) out.push({ name: f.name, rel: f.rel });
    }
  }
  return out;
}

function screenshotsPane(shots) {
  if (!shots.length) {
    return el('div', { class: 'empty' },
      'No screenshots. They are captured at each step of a browser run, so a run that never opened a page has none.');
  }
  const wrap = el('div', {});
  for (const s of shots) {
    const holder = el('div', { class: 'dim small' }, 'Loading…');
    wrap.appendChild(el('div', { class: 'card u-mb-14px' },
      el('h2', {}, stepLabel(s.name), el('span', { class: 'sub' }, s.name)),
      holder));
    /* Every load starts at once but paints into the slot it belongs to, so the
     * steps stay in the order they happened, not the order they came back. */
    (async () => {
      try {
        const url = await artifactBlob(s.rel);
        clear(holder).appendChild(el('img', { class: 'shot', src: url, alt: stepLabel(s.name) }));
      } catch (e) {
        clear(holder).appendChild(el('div', { class: 'banner bad' },
          `Could not load ${s.name}: ${e.message}`));
      }
    })();
  }
  return wrap;
}

function stepLabel(name) {
  const base = String(name).replace(/\.[^.]+$/, '');
  const m = /^(step|captcha)-(\d+)$/.exec(base);
  if (m) return `${m[1] === 'step' ? 'Step' : 'CAPTCHA at step'} ${m[2]}`;
  if (base === 'final') return 'Final page';
  return base;
}

/* --------------------------------------------------------------- timeline -- */
function timelinePane(events) {
  if (!events.length) {
    return el('div', { class: 'empty' }, 'No events were logged against this job.');
  }
  const tb = el('tbody');
  for (const e of events) {
    tb.appendChild(el('tr', {},
      /* The exact stamp is on the title so the relative time stays readable
       * without losing the precision that a dispute would need. */
      el('td', { class: 'shrink dim small mono', title: e.at || '' }, fmtAgo(e.at)),
      el('td', { class: 'shrink' },
        pill(e.kind, e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : '')),
      el('td', { class: 'small muted' }, e.message || ''),
    ));
  }
  return el('div', { class: 'scroll-y' }, el('table', {}, tb));
}

/* ----------------------------------------------------------------- shared -- */
function baseName(p) {
  if (!p) return '';
  return String(p).split(/[\\/]/).pop();
}

function parseJsonList(s) {
  if (Array.isArray(s)) return s;
  if (typeof s !== 'string' || !s) return [];
  try {
    const v = JSON.parse(s);
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}
