/* Apply — paste a link, or hand the whole loop over.
 *
 * Two ways in, in the order Ali uses them: a single posting he found himself,
 * then the unattended runner. The autonomy picker sits with the link box
 * rather than in Settings because the honest question — "is this one going to
 * be submitted?" — is asked per application, not once per install.
 *
 * Note on layout: style-src is 'self' with no 'unsafe-inline', so a style=""
 * attribute is dropped by the browser. Every one below is cosmetic spacing;
 * nothing here needs one to stay usable.
 */

import {
  el, clear, get, post, runTask, watchTask, pill, toast, go } from '../app.js';

const POLL_MS = 5000;

function fmtElapsed(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

/* A task's result may be a string, a small object, or nothing at all, and an
 * error matters more than a result when both are present. */
function outcomeText(t) {
  if (t.error) return t.error;
  if (t.result === null || t.result === undefined || t.result === '') return '';
  return typeof t.result === 'string' ? t.result : JSON.stringify(t.result);
}

export async function render(ctx) {
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => go('jobs') }, 'Browse jobs'),
    el('button', { class: 'btn', onclick: () => go('settings') }, 'Default mode'),
  );

  ctx.view.appendChild(el('div', { class: 'empty' }, 'Loading…'));

  let settings;
  let tasks;
  try {
    [settings, tasks] = await Promise.all([get('/api/settings'), get('/api/tasks')]);
  } catch (e) {
    clear(ctx.view).appendChild(
      el('div', { class: 'banner bad' }, `Could not reach the server: ${e.message}`));
    return;
  }
  clear(ctx.view);

  const levels = settings.autonomy?.levels || [];
  const configured = settings.autonomy?.level || '';
  const configuredLabel = levels.find((l) => l.key === configured)?.label || configured;
  const dailyCap = settings.settings?.apply?.max_per_day;

  /* ------------------------------------------------------- apply to a link */
  let chosen = levels.some((l) => l.key === configured) ? configured : levels[0]?.key;

  const urlInput = el('input', {
    type: 'url', autocomplete: 'off', spellcheck: 'false',
    placeholder: 'https://job-boards.greenhouse.io/...',
    onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); submitLink(); } },
    oninput: () => clear(errHost) });
  const errHost = el('div', {});
  const applyBtn = el('button', { class: 'btn btn-primary', onclick: () => submitLink() }, 'Apply');

  const note = el('div', { class: 'small muted u-mt-10px' });
  const picker = el('div', { class: 'levels' });
  for (const l of levels) {
    picker.appendChild(el('label', {
      class: `level ${l.key === chosen ? 'on' : ''}`, dataset: { key: l.key } },
    el('input', {
      type: 'radio', name: 'apply-level', checked: l.key === chosen,
      onchange: () => {
        chosen = l.key;
        for (const card of picker.children) card.classList.toggle('on', card.dataset.key === l.key);
        paintNote();
      } }),
    el('div', {},
      el('div', { class: 'lv-label' }, l.label),
      el('div', { class: 'lv-blurb' }, l.blurb)),
    el('span', { class: 'lv-tag' }, l.submits ? pill('submits', 'warn') : pill('never submits', 'ok'))));
  }

  function paintNote() {
    const l = levels.find((x) => x.key === chosen);
    note.textContent = l?.submits
      ? 'This will actually submit the application.'
      : 'It will stop before submitting.';
  }
  paintNote();

  function showUrlError(message) {
    clear(errHost).appendChild(el('div', { class: 'banner bad u-mt-10px' }, message));
  }

  async function submitLink() {
    clear(errHost);
    const url = urlInput.value.trim();
    // The server checks this too. Checking here means a typo costs nothing and
    // the complaint lands next to the field that caused it.
    if (!/^https?:\/\//i.test(url)) {
      showUrlError('Paste the full link to the posting — it has to start with http:// or https://');
      urlInput.focus();
      return;
    }
    if (!chosen) {
      showUrlError('Choose a mode below first.');
      return;
    }
    applyBtn.disabled = true;
    try {
      await runTask('/api/apply', { url, level: chosen }, { onDone: ctx.refresh });
    } catch (e) {
      // Usually a 409: an apply is already running. That is worth leaving on
      // screen rather than putting in a toast that fades behind the drawer.
      showUrlError(e.message);
    } finally {
      applyBtn.disabled = false;
    }
  }

  ctx.view.appendChild(el('div', { class: 'card' },
    el('h2', {}, 'Apply to a link', el('span', { class: 'sub' }, 'one posting, start to finish')),
    el('div', {}, urlInput),
    el('div', { class: 'row u-mt-10px' },
      applyBtn,
      el('span', { class: 'dim small' }, 'Greenhouse, Lever, Workday, Ashby, or a plain careers page.')),
    errHost,
    el('div', { class: 'u-mt-14px' }, picker),
    note));

  /* ---------------------------------------------------------- run on its own */
  const maxApps = el('input', {
    type: 'number', min: '0', max: '200', step: '1', value: '0', class: 'u-w-90px' });
  const onceBox = el('input', { type: 'checkbox' });
  const runControls = el('div', { class: 'row u-mt-12px' });

  async function startRun(once) {
    const body = { max_apps: Number(maxApps.value) || 0, once };
    // Sending the configured level explicitly makes the drawer's first line
    // state which mode the loop is running at, instead of leaving it implied.
    if (configured) body.level = configured;
    try {
      await runTask('/api/run', body, { onDone: ctx.refresh });
      paintTasks(await get('/api/tasks'));
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  ctx.view.appendChild(el('div', { class: 'card' },
    el('h2', {}, 'Run on its own', el('span', { class: 'sub' }, 'unattended')),
    el('p', { class: 'muted small' },
      'It discovers new postings, scores them, tailors the resume and works through the '
      + 'queue on a loop until you stop it.'),
    el('div', { class: 'row' },
      el('label', { class: 'inline' },
        el('span', {}, 'Stop after'), maxApps,
        el('span', { class: 'dim small' },
          dailyCap
            ? `applications (0 = the daily cap, ${dailyCap})`
            : 'applications (0 = the daily cap)')),
      el('label', { class: 'inline' }, onceBox, 'One cycle only')),
    runControls));

  /* -------------------------------------------------------------- recent runs */
  const runsBody = el('div', {});
  const runsCard = el('div', { class: 'card' },
    el('h2', {}, 'Recent runs', el('span', { class: 'sub' }, 'click one to reopen its log')),
    runsBody);
  ctx.view.appendChild(runsCard);

  function paintRunControls(list) {
    const live = list.find((t) => t.kind === 'run' && t.status === 'running');
    maxApps.disabled = Boolean(live);
    onceBox.disabled = Boolean(live);
    clear(runControls);
    if (!live) {
      runControls.append(
        el('button', { class: 'btn btn-primary', onclick: () => startRun(onceBox.checked) }, 'Start'),
        el('button', { class: 'btn', onclick: () => startRun(true) }, 'One cycle'),
        el('span', { class: 'dim small' },
          configured ? `Runs at your default mode: ${configuredLabel}.` : 'Runs at the mode set in Settings.'));
      return;
    }
    runControls.append(
      el('button', { class: 'btn', onclick: () => watchTask(live, { onDone: ctx.refresh }) }, 'Watching'),
      el('button', {
        class: 'btn btn-danger',
        onclick: async () => {
          try {
            await post(`/api/tasks/${live.id}/stop`);
            toast('Stopping after the current application…', 'info');
          } catch (e) { toast(e.message, 'bad'); }
        } }, 'Stop'),
      pill('running', 'info'),
      el('span', { class: 'dim small mono' }, fmtElapsed(live.elapsed)));
  }

  function paintRunsTable(list) {
    clear(runsBody);
    if (!list.length) {
      runsBody.appendChild(el('div', { class: 'empty' },
        'Nothing has run yet. Paste a link above, or start the runner.'));
      return;
    }
    const tb = el('tbody');
    for (const t of list) {
      tb.appendChild(el('tr', { class: 'clickable', onclick: () => watchTask(t, { onDone: ctx.refresh }) },
        el('td', {}, t.name),
        el('td', { class: 'shrink' }, pill(t.kind)),
        el('td', { class: 'shrink' }, pill(t.status, t.status === 'stopped' ? 'info' : null)),
        el('td', { class: 'shrink dim small mono' }, fmtElapsed(t.elapsed)),
        el('td', { class: t.error ? 'small' : 'small muted' }, outcomeText(t)),
      ));
    }
    runsBody.appendChild(el('div', { class: 'scroll-y' }, el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Task'),
        el('th', { class: 'shrink' }, 'Kind'),
        el('th', { class: 'shrink' }, 'Status'),
        el('th', { class: 'shrink' }, 'Took'),
        el('th', {}, 'Result'))),
      tb)));
  }

  function paintTasks(payload) {
    const list = payload?.tasks || [];
    paintRunControls(list);
    paintRunsTable(list);
  }
  paintTasks(tasks);

  /* The shell reuses one #view element for every panel, so ctx.view is never
   * disconnected. A node this render owns is the only reliable signal that the
   * panel has been torn down or re-rendered under us. */
  const timer = setInterval(async () => {
    if (!ctx.view.isConnected || !runsCard.isConnected) { clearInterval(timer); return; }
    let fresh;
    try {
      fresh = await get('/api/tasks');
    } catch {
      return; // a dropped poll is not worth a banner; the next tick retries
    }
    if (!runsCard.isConnected) { clearInterval(timer); return; }
    paintTasks(fresh);
  }, POLL_MS);
}
