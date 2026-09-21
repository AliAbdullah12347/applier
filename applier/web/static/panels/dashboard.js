/* Dashboard — what is happening, and what to do next.
 *
 * Ordered by what actually blocks progress rather than by what is easy to
 * count. Anything waiting on Ali comes first: an unanswered question stops an
 * application dead, and a number further down the page does not.
 */

import {
  el, clear, get, post, runTask, pill, scoreCell, fmtAgo, toast, go, refreshChrome } from '../app.js';

function stat(n, label, kind) {
  return el('div', { class: `stat ${kind || ''}` },
    el('div', { class: 'n' }, String(n ?? 0)),
    el('div', { class: 'k' }, label));
}

export async function render(ctx) {
  const s = await get('/api/state');

  /* ---------------------------------------------------------- top actions */
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => runTask('/api/discover', {}, { onDone: ctx.refresh }) },
      'Find jobs'),
    el('button', { class: 'btn', onclick: () => runTask('/api/rank', {}, { onDone: ctx.refresh }) },
      'Re-score'),
    el('button', { class: 'btn btn-primary', onclick: () => go('apply') }, 'Apply to a link'),
  );

  /* -------------------------------------------------------- what's blocked */
  const blockers = [];

  if (s.open_asks) {
    blockers.push(banner('warn', `${s.open_asks} question${s.open_asks > 1 ? 's' : ''} waiting on you.`,
      'Answer them', () => go('asks')));
  }
  if (s.funnel.needs_input) {
    blockers.push(banner('warn',
      `${s.funnel.needs_input} application${s.funnel.needs_input > 1 ? 's' : ''} paused — a CAPTCHA or a field it would not guess.`,
      'Review', () => go('applications')));
  }
  if (s.profile_gaps?.length) {
    blockers.push(banner('warn',
      `${s.profile_gaps.length} profile field${s.profile_gaps.length > 1 ? 's' : ''} still unanswered: ${s.profile_gaps.slice(0, 3).join(', ')}${s.profile_gaps.length > 3 ? '…' : ''}`,
      'Fill them in', () => go('settings')));
  }
  if (s.llm_failures > 0 && s.llm_calls > 0 && s.llm_failures / s.llm_calls > 0.25) {
    blockers.push(banner('bad',
      `${s.llm_failures} of ${s.llm_calls} model calls failed. The key may be rejected or out of quota.`,
      'Run doctor', async () => { await runDoctor(); }));
  }
  if (!s.funnel.submitted && s.funnel.started) {
    blockers.push(banner('warn',
      `${s.funnel.started} application${s.funnel.started > 1 ? 's' : ''} prepared, none submitted yet.`,
      'See them', () => go('applications')));
  }
  blockers.forEach((b) => ctx.view.appendChild(b));

  /* ----------------------------------------------------------- the funnel */
  const f = s.funnel;
  ctx.view.appendChild(el('div', { class: 'grid c4' },
    stat(f.discovered, 'Discovered'),
    stat(f.queued, 'Queued', f.queued ? 'good' : ''),
    stat(f.started, 'Started'),
    stat(f.submitted, 'Submitted', f.submitted ? 'good' : 'warn'),
  ));

  ctx.view.appendChild(el('div', { class: 'grid c4' },
    stat(f.gate_rejected, 'Gate-rejected'),
    stat(s.open_asks, 'Open questions', s.open_asks ? 'warn' : ''),
    stat(s.answers_learned, 'Answers learned'),
    stat(s.contacts, 'Contacts'),
  ));

  /* ---------------------------------------------------------- the outcomes */
  const o = s.outcomes || {};
  const anyOutcome = Object.keys(o).length > 0;
  if (anyOutcome) {
    ctx.view.appendChild(el('div', { class: 'grid c4' },
      stat(o.interview || 0, 'Interviews', o.interview ? 'good' : ''),
      stat(o.oa || 0, 'Assessments'),
      stat(o.rejection || 0, 'Rejections', o.rejection ? 'bad' : ''),
      stat(o.offer || 0, 'Offers', o.offer ? 'good' : ''),
    ));
  }

  /* ---------------------------------------------------- autonomy + running */
  const lvl = s.autonomy.levels.find((l) => l.key === s.autonomy.level);
  const autonomyCard = el('div', { class: 'card' },
    el('h2', {}, 'Autonomy', el('span', { class: 'sub' }, 'how much it does without you')),
    el('div', { class: 'levels' },
      ...s.autonomy.levels.map((l) => el('label', { class: `level ${l.key === s.autonomy.level ? 'on' : ''}` },
        el('input', {
          type: 'radio', name: 'autonomy', checked: l.key === s.autonomy.level,
          onchange: async () => {
            try {
              const r = await post('/api/autonomy', { level: l.key });
              toast(`Mode: ${r.label}`, 'ok');
              await refreshChrome();
              ctx.refresh();
            } catch (e) { toast(e.message, 'bad'); }
          } }),
        el('div', {},
          el('div', { class: 'lv-label' }, l.label),
          el('div', { class: 'lv-blurb' }, l.blurb)),
        l.submits ? pill('submits', 'warn') : pill('never submits', 'ok'),
      ))),
    el('div', { class: 'row u-mt-12px' },
      el('button', {
        class: 'btn btn-primary',
        onclick: () => runTask('/api/run', { once: false }, { onDone: ctx.refresh }) }, 'Start autonomous run'),
      el('button', {
        class: 'btn',
        onclick: () => runTask('/api/run', { once: true }, { onDone: ctx.refresh }) }, 'One cycle only'),
      el('span', { class: 'dim small' },
        lvl?.submits ? 'This mode submits applications.' : 'This mode never submits.')),
  );

  /* ------------------------------------------------------------- best next */
  const topCard = el('div', { class: 'card' },
    el('h2', {}, 'Best matches waiting', el('span', { class: 'sub' }, 'highest scoring, not yet applied')));
  if (!s.top?.length) {
    topCard.appendChild(el('div', { class: 'empty' }, 'Nothing queued. Run "Find jobs".'));
  } else {
    const tb = el('tbody');
    for (const j of s.top) {
      tb.appendChild(el('tr', { class: 'clickable', onclick: () => go('jobs', { id: j.id }) },
        el('td', { class: 'num shrink' }, scoreCell(j.score)),
        el('td', {}, el('div', {}, j.company),
          el('div', { class: 'dim small' }, j.title)),
        el('td', { class: 'shrink dim small' }, j.location || '—'),
      ));
    }
    topCard.appendChild(el('table', {}, tb));
  }

  ctx.view.appendChild(el('div', { class: 'grid c2' }, autonomyCard, topCard));

  /* ------------------------------------------------------------- activity */
  const actCard = el('div', { class: 'card' },
    el('h2', {}, 'Recent activity',
      el('span', { class: 'sub' }, `${s.llm_calls} model calls, ${s.llm_failures} failed`)));
  if (!s.recent?.length) {
    actCard.appendChild(el('div', { class: 'empty' }, 'Nothing has happened yet.'));
  } else {
    const tb = el('tbody');
    for (const e of s.recent) {
      tb.appendChild(el('tr', {},
        el('td', { class: 'shrink dim small mono' }, fmtAgo(e.at)),
        el('td', { class: 'shrink' }, pill(e.kind, e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : '')),
        el('td', { class: 'small muted' }, e.message || ''),
      ));
    }
    actCard.appendChild(el('div', { class: 'scroll-y' }, el('table', {}, tb)));
  }
  ctx.view.appendChild(actCard);

  /* ---------------------------------------------------------------- doctor */
  const docCard = el('div', { class: 'card' },
    el('h2', {}, 'System check', el('span', { class: 'sub' }, 'keys, dependencies, a real model call')));
  const docBody = el('div', {}, el('div', { class: 'empty' }, 'Not run yet this session.'));
  docCard.append(
    el('div', { class: 'row u-mb-10px' },
      el('button', { class: 'btn', onclick: runDoctor }, 'Run check')),
    docBody);
  ctx.view.appendChild(docCard);

  async function runDoctor() {
    clear(docBody).appendChild(el('div', { class: 'empty' }, 'Checking… (the live model call takes a few seconds)'));
    try {
      const r = await post('/api/doctor');
      clear(docBody);
      for (const c of r.checks) {
        docBody.appendChild(el('div', { class: `check ${c.ok ? 'ok' : 'bad'}` },
          el('span', { class: 'mark' }, c.ok ? '✓' : '✕'),
          el('div', {}, el('div', {}, c.name), el('div', { class: 'detail' }, c.detail))));
      }
      toast(r.ok ? 'All checks passed' : 'Some checks failed', r.ok ? 'ok' : 'bad');
    } catch (e) {
      clear(docBody).appendChild(el('div', { class: 'banner bad' }, e.message));
    }
  }
}

function banner(kind, message, actionLabel, onClick) {
  return el('div', { class: `banner ${kind}` },
    el('span', {}, message),
    el('span', { class: 'spacer' }),
    el('button', { class: 'btn btn-sm', onclick: onClick }, actionLabel));
}
