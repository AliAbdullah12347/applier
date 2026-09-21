/* Jobs — the queue of discovered postings, and one posting in full.
 *
 * The list is ordered by score by default because that is the only column that
 * says "spend your evening here". Everything else on a row exists to explain
 * why a job is *not* worth opening: a gate rejection with its reason, or a
 * posting so thin the gates could not run at all. A row that looks fine and
 * carries no warning is the exception, not the norm.
 *
 * Company names, titles and gate reasons all originate on employer pages, so
 * every one of them goes in as text through el(); nothing here builds markup.
 */

import {
  el, clear, get, post, runTask, pill, scoreCell, fmtDate, fmtAgo, toast, modal, go,
  link as safeLink } from '../app.js';

const PAGE = 50;

const SORTS = [
  ['score', 'best score'],
  ['new', 'newest found'],
  ['company', 'company A–Z'],
  ['seen', 'last seen'],
];

export async function render(ctx) {
  if (ctx.params?.id) return detail(ctx, ctx.params.id);
  return list(ctx);
}

/* --------------------------------------------------------------- the list -- */
async function list(ctx) {
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => runTask('/api/discover', {}, { onDone: ctx.refresh }) },
      'Find jobs'),
    el('button', { class: 'btn', onclick: () => runTask('/api/rank', {}, { onDone: ctx.refresh }) },
      'Re-score'),
  );

  /* Filter state lives in this closure rather than in the URL. Re-rendering the
   * whole panel on every keystroke would rebuild the search box and throw focus
   * out of it mid-word, so only the table below is redrawn. */
  const q = { status: 'all', q: '', sort: 'score', offset: 0, limit: PAGE };

  let typing = null;
  const search = el('input', {
    type: 'text', placeholder: 'Company, title or location',
    oninput: () => {
      clearTimeout(typing);
      typing = setTimeout(() => { q.q = search.value.trim(); q.offset = 0; load(); }, 250);
    } });

  const statusSel = el('select', {
    onchange: () => { q.status = statusSel.value; q.offset = 0; load(); } }, el('option', { value: 'all' }, 'all'));

  const sortSel = el('select', {
    onchange: () => { q.sort = sortSel.value; q.offset = 0; load(); } }, ...SORTS.map(([k, label]) => el('option', { value: k }, label)));
  sortSel.value = q.sort;

  const tally = el('span', { class: 'dim small' });

  ctx.view.appendChild(el('div', { class: 'card' },
    el('div', { class: 'filters' },
      search, statusSel, sortSel, el('span', { class: 'spacer' }), tally)));

  const host = el('div', { class: 'card' });
  ctx.view.appendChild(host);

  /* The counts come back with the list, so the dropdown is rebuilt each load.
   * A status whose last job just moved away is kept in the options anyway —
   * dropping it would silently reset the filter and make the table look wrong. */
  function fillStatus(counts) {
    const total = Object.values(counts).reduce((a, b) => a + Number(b || 0), 0);
    const keys = Object.keys(counts).sort();
    if (q.status !== 'all' && !keys.includes(q.status)) keys.push(q.status);
    clear(statusSel);
    statusSel.appendChild(el('option', { value: 'all' }, `all (${total})`));
    for (const k of keys) {
      statusSel.appendChild(el('option', { value: k }, `${k} (${counts[k] || 0})`));
    }
    statusSel.value = q.status;
  }

  async function load() {
    clear(host).appendChild(el('div', { class: 'empty' }, 'Loading postings…'));

    let data;
    try {
      const p = new URLSearchParams({
        status: q.status, sort: q.sort,
        limit: String(q.limit), offset: String(q.offset) });
      if (q.q) p.set('q', q.q);
      data = await get(`/api/jobs?${p.toString()}`);
    } catch (e) {
      clear(host).appendChild(el('div', { class: 'banner bad' }, `Could not load jobs: ${e.message}`));
      return;
    }

    const jobs = data.jobs || [];
    const total = data.total || 0;
    fillStatus(data.counts || {});
    clear(tally).appendChild(document.createTextNode(
      total ? `${total} match${total === 1 ? '' : 'es'}` : 'no matches'));
    clear(host);

    if (!jobs.length) {
      const filtered = q.q || q.status !== 'all';
      host.appendChild(el('div', { class: 'empty' }, filtered
        ? 'No posting matches that filter. Clear the search box or choose a different status.'
        : 'Nothing discovered yet. Press "Find jobs" above to search the boards, then "Re-score" to rank what comes back.'));
      return;
    }

    const tb = el('tbody');
    for (const j of jobs) tb.appendChild(row(j));
    host.appendChild(el('table', {},
      el('thead', {}, el('tr', {},
        el('th', { class: 'shrink' }, 'Score'),
        el('th', {}, 'Role'),
        el('th', { class: 'shrink' }, 'Location'),
        el('th', { class: 'shrink' }, 'Source'),
        el('th', { class: 'shrink' }, 'Gate'),
        el('th', { class: 'shrink' }, ''),
      )),
      tb));

    const from = data.offset + 1;
    const to = data.offset + jobs.length;
    host.appendChild(el('div', { class: 'row u-mt-12px' },
      el('button', {
        class: 'btn btn-sm', disabled: data.offset === 0,
        onclick: () => { q.offset = Math.max(0, data.offset - q.limit); load(); } }, 'Previous'),
      el('button', {
        class: 'btn btn-sm', disabled: to >= total,
        onclick: () => { q.offset = data.offset + q.limit; load(); } }, 'Next'),
      el('span', { class: 'dim small' }, `${from}–${to} of ${total}`)));
  }

  function row(j) {
    const title = el('td', {},
      el('div', {}, j.company || 'Unknown company'),
      el('div', { class: 'dim small' }, j.title || ''),
      j.gate_status === 'rejected' && j.gate_reason
        ? el('div', { class: 'dim small' }, `gate: ${j.gate_reason}`)
        : null);

    const loc = el('td', { class: 'shrink dim small' }, j.location || '—');
    if (j.remote) loc.appendChild(el('span', { class: 'u-ml-6px' }, pill('remote', 'info')));

    const origin = j.ats || j.source || 'unknown';
    const src = el('td', { class: 'shrink' },
      el('span', { class: 'pill', title: `source: ${j.source || '—'} · ats: ${j.ats || 'unknown'}` },
        origin));

    const gate = el('td', { class: 'shrink' },
      j.gate_status ? pill(j.gate_status) : el('span', { class: 'dim' }, 'not run'));
    if (j.thin) {
      gate.appendChild(el('span', { class: 'u-ml-6px' },
        el('span', {
          class: 'pill warn',
          title: 'The posting body was never captured, so the eligibility gates could not be run against it. Open the posting to judge it yourself.' }, 'no description')));
    }

    /* One stopPropagation on the cell instead of one per button: every control
     * in here is an action, and none of them should also open the row. */
    const acts = el('td', { class: 'shrink', onclick: (e) => e.stopPropagation() });
    if (j.application_id) {
      acts.appendChild(el('a', {
        class: 'u-cur-pointer', onclick: () => go('applications', { id: j.application_id }) }, pill(j.application_status || 'started')));
    } else {
      acts.appendChild(el('div', { class: 'row tight' },
        el('button', { class: 'btn btn-sm', onclick: () => applyModal(j, load) }, 'Apply'),
        j.status === 'skipped'
          ? el('button', { class: 'btn btn-sm', onclick: () => setStatus(j.id, 'queued', load) }, 'Re-queue')
          : el('button', { class: 'btn btn-sm', onclick: () => setStatus(j.id, 'skipped', load) }, 'Skip')));
    }

    return el('tr', { class: 'clickable', onclick: () => go('jobs', { id: j.id }) },
      el('td', { class: 'num shrink' }, scoreCell(j.score)),
      title, loc, src, gate, acts);
  }

  await load();
}

/* ------------------------------------------------------------- one posting -- */
async function detail(ctx, id) {
  ctx.actions.appendChild(el('button', { class: 'btn', onclick: () => go('jobs') }, 'Back to list'));
  ctx.view.appendChild(el('div', { class: 'empty' }, 'Loading posting…'));

  let job;
  try {
    ({ job } = await get(`/api/jobs/${encodeURIComponent(id)}`));
  } catch (e) {
    clear(ctx.view).appendChild(el('div', { class: 'banner bad' }, `Could not load this posting: ${e.message}`));
    return;
  }
  clear(ctx.view);

  const head = el('div', { class: 'card' },
    el('h2', {}, job.company || 'Unknown company',
      el('span', { class: 'sub' }, job.title || '')),
    el('dl', { class: 'kv' },
      ...kv('Location', job.location || '—'),
      ...kv('Remote', job.remote ? 'yes' : 'no'),
      ...kv('Score', scoreCell(job.score)),
      ...kv('Status', pill(job.status)),
      ...kv('Source', `${job.source || '—'} · ${job.ats || 'unknown ATS'}`),
      ...kv('Posted', fmtDate(job.posted_at)),
      ...kv('First seen', `${fmtDate(job.first_seen_at)} (${fmtAgo(job.first_seen_at)})`),
    ),
    el('div', { class: 'row u-mt-12px' },
      job.url ? link(job.url, 'Open posting') : null,
      job.apply_url && job.apply_url !== job.url ? link(job.apply_url, 'Open application form') : null),
  );
  ctx.view.appendChild(head);

  /* The verdict goes above everything else: if the gates rejected this, the
   * reason is the only thing on the page worth reading. */
  if (job.gate_status === 'pass') {
    ctx.view.appendChild(el('div', { class: 'banner ok' },
      el('span', {}, 'Eligibility gates passed. Nothing in this posting rules you out.')));
  } else if (job.gate_status === 'rejected') {
    ctx.view.appendChild(el('div', { class: 'banner bad' },
      el('span', {}, `Gate rejected: ${job.gate_reason || 'no reason recorded'}`)));
  } else {
    ctx.view.appendChild(el('div', { class: 'banner' },
      el('span', {}, 'The eligibility gates have not run on this posting yet.'),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm', onclick: () => runTask('/api/rank', {}, { onDone: ctx.refresh }) },
        'Re-score')));
  }

  if (job.sponsorship) {
    ctx.view.appendChild(el('div', { class: 'card' },
      el('h2', {}, 'Sponsorship', el('span', { class: 'sub' }, 'what the posting says about visas')),
      pill(job.sponsorship,
        job.sponsorship === 'sponsors' ? 'ok' : job.sponsorship === 'refuses' ? 'bad' : 'warn')));
  }

  /* Score breakdown — a single number nobody can decompose is a number nobody
   * trusts, so every component the ranker produced is listed. */
  const parts = Object.entries(job.score_detail || {});
  const scoreCard = el('div', { class: 'card' },
    el('h2', {}, 'Score breakdown', el('span', { class: 'sub' }, 'what went into the number')));
  if (!parts.length) {
    scoreCard.appendChild(el('div', { class: 'empty' },
      'No breakdown stored. Run "Re-score" to score this posting with the current profile.'));
  } else {
    scoreCard.appendChild(el('dl', { class: 'kv' },
      ...parts.flatMap(([k, v]) => kv(k,
        typeof v === 'number' ? el('span', { class: 'mono' }, v.toFixed(2)) : String(v)))));
  }
  ctx.view.appendChild(scoreCard);

  const jdCard = el('div', { class: 'card' },
    el('h2', {}, 'Description', el('span', { class: 'sub' }, 'as captured from the posting')));
  if (job.description && job.description.trim()) {
    jdCard.appendChild(el('div', { class: 'jd' }, job.description));
  } else {
    jdCard.appendChild(el('div', { class: 'empty' },
      'No description was captured. The gates cannot judge this one — open the posting and read it yourself before applying.'));
  }
  ctx.view.appendChild(jdCard);

  const actCard = el('div', { class: 'card' },
    el('h2', {}, 'Actions'));
  if (job.application) {
    actCard.append(
      el('div', { class: 'row u-mb-10px' },
        el('span', { class: 'muted' }, 'An application already exists for this posting.'),
        pill(job.application.status)),
      el('div', { class: 'row' },
        el('button', {
          class: 'btn btn-primary',
          onclick: () => go('applications', { id: job.application.id }) }, 'Open the application')));
  } else {
    actCard.appendChild(el('div', { class: 'row' },
      el('button', { class: 'btn btn-primary', onclick: () => applyModal(job, ctx.refresh) }, 'Apply'),
      job.status === 'skipped'
        ? el('button', { class: 'btn', onclick: () => setStatus(job.id, 'queued', ctx.refresh) }, 'Re-queue')
        : el('button', { class: 'btn', onclick: () => setStatus(job.id, 'skipped', ctx.refresh) }, 'Skip'),
      job.status !== 'queued' && job.status !== 'skipped'
        ? el('button', { class: 'btn', onclick: () => setStatus(job.id, 'queued', ctx.refresh) }, 'Queue')
        : null));
  }
  ctx.view.appendChild(actCard);
}

/* ------------------------------------------------------------------ bits -- */
function kv(label, value) {
  return [el('dt', {}, label), el('dd', {}, value)];
}

/* Job URLs come from ATS feeds, so they go through the shell's scheme check
 * rather than straight into an href. */
function link(href, label) {
  return safeLink(href, label, { class: 'btn btn-sm u-td-none' });
}

async function setStatus(jobId, status, after) {
  try {
    await post(`/api/jobs/${encodeURIComponent(jobId)}/status`, { status });
    toast(`Marked ${status}`, 'ok');
    await after?.();
  } catch (e) {
    toast(e.message, 'bad');
  }
}

/* Applying is the one irreversible thing on this screen, so it always goes
 * through the level picker — including from a list row. The extra click buys
 * an explicit answer to "is this allowed to press submit?". */
async function applyModal(job, onDone) {
  let levels = [];
  let chosen = null;
  try {
    const s = await get('/api/settings');
    levels = s.autonomy?.levels || [];
    chosen = s.autonomy?.level || levels[0]?.key || null;
  } catch (e) {
    toast(`Could not read the autonomy levels: ${e.message}`, 'bad');
    return;
  }
  if (!levels.length) {
    toast('No autonomy levels are configured.', 'bad');
    return;
  }

  const note = el('div', { class: 'small u-mt-10px' });
  const rows = [];

  function repaint() {
    for (const { wrap, lvl } of rows) wrap.classList.toggle('on', lvl.key === chosen);
    const lvl = levels.find((l) => l.key === chosen);
    clear(note).appendChild(lvl?.submits
      ? el('div', { class: 'banner warn' }, 'This level can press submit without asking again.')
      : el('div', { class: 'muted' }, 'This level never submits. You stay in control of the final click.'));
  }

  const picker = el('div', { class: 'levels' });
  for (const lvl of levels) {
    const wrap = el('label', { class: 'level' },
      el('input', {
        type: 'radio', name: 'apply-level', checked: lvl.key === chosen,
        onchange: () => { chosen = lvl.key; repaint(); } }),
      el('div', {},
        el('div', { class: 'lv-label' }, lvl.label),
        el('div', { class: 'lv-blurb' }, lvl.blurb)),
      lvl.submits ? pill('submits', 'warn') : pill('never submits', 'ok'));
    rows.push({ wrap, lvl });
    picker.appendChild(wrap);
  }
  repaint();

  const body = el('div', {},
    el('p', { class: 'muted' }, `${job.company || 'Unknown company'} — ${job.title || ''}`),
    picker, note);

  modal('Apply — how much should it do?', body, [{
    label: 'Apply', primary: true,
    onClick: async () => {
      try {
        await runTask(`/api/jobs/${encodeURIComponent(job.id)}/apply`, { level: chosen }, { onDone });
      } catch (e) {
        // Keep the dialog open on failure: the usual cause is an application
        // that already exists, and closing would hide why nothing happened.
        toast(e.message, 'bad');
        return true;
      }
    } }]);
}
