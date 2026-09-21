/* Questions — the queue of things the system could not answer, and the bank
 * of things it already knows.
 *
 * The contract with Ali is "answer once, never again", so the two tabs are
 * really one idea seen twice: the queue is the bank's inbox. Everything that
 * lands here came off an employer's page, so every string on this screen is
 * treated as untrusted text and goes in through el(), never markup.
 */

import {
  el, clear, get, post, patch, del, pill, fmtAgo, toast, confirmDialog, go, refreshChrome } from '../app.js';

/* Survives ctx.refresh() so that saving an answer from the second tab does
 * not bounce you back to the first one. */
let activeTab = 'queue';

export async function render(ctx) {
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => ctx.refresh() }, 'Reload'),
  );

  const body = el('div', {});
  const tabs = el('div', { class: 'tabs' });
  const spec = [
    ['queue', 'Waiting on you', renderQueue],
    ['bank', 'Remembered answers', renderBank],
  ];

  for (const [id, label, draw] of spec) {
    tabs.appendChild(el('button', {
      class: id === activeTab ? 'on' : '',
      onclick: () => {
        if (activeTab === id) return;
        activeTab = id;
        for (const b of tabs.children) b.classList.toggle('on', b.dataset.tab === id);
        draw(clear(body), ctx);
      },
      dataset: { tab: id } }, label));
  }

  ctx.view.append(tabs, body);
  const current = spec.find((s) => s[0] === activeTab) || spec[0];
  await current[2](body, ctx);
}

/* ------------------------------------------------------------ the queue -- */
async function renderQueue(host, ctx) {
  host.appendChild(el('div', { class: 'empty' }, 'Loading the queue…'));

  let data;
  try {
    data = await get('/api/asks');
  } catch (e) {
    clear(host).appendChild(el('div', { class: 'banner bad' }, e.message));
    return;
  }

  clear(host);
  host.appendChild(el('div', { class: 'dim small' },
    'Answer a question once and it is remembered for every later application.'));

  const list = el('div', { class: 'grid' });
  const asks = data.asks || [];
  let remaining = asks.length;

  const emptyNote = () => el('div', { class: 'empty' },
    'Nothing waiting. Anything the system cannot resolve will appear here.');

  if (!asks.length) {
    list.appendChild(emptyNote());
  } else {
    for (const a of asks) list.appendChild(askCard(a, onGone));
  }
  host.appendChild(list);

  if (data.profile_gaps?.length) host.appendChild(gapsCard(data.profile_gaps));

  /* Cards are removed in place rather than re-rendering, so that clearing a
   * long queue does not scroll you back to the top after every answer. */
  function onGone() {
    remaining -= 1;
    if (remaining <= 0) clear(list).appendChild(emptyNote());
    refreshChrome();
  }
}

function askCard(a, onGone) {
  const card = el('div', { class: 'card' });
  const where = a.company ? `${a.company} — ${a.title || 'role unknown'}` : 'general';
  const question = a.question_text || '(blank question)';

  const input = controlFor(a, question);
  const saveBtn = el('button', { class: 'btn btn-primary' }, 'Save');
  const dropBtn = el('button', { class: 'btn btn-ghost' }, 'Dismiss');

  /* A slot rather than a styled span: the CSP forbids inline styles, so the
   * only reliable way to make an error look like an error is the banner. */
  const err = el('div', {});
  const fail = (msg) => clear(err).appendChild(el('div', { class: 'banner bad' }, msg));

  saveBtn.onclick = async () => {
    const value = String(input.value || '').trim();
    if (!value) {
      fail('An answer is required.');
      input.focus();
      return;
    }
    clear(err);
    saveBtn.disabled = true;
    dropBtn.disabled = true;
    try {
      await post(`/api/asks/${a.id}`, { answer: value });
      toast('Saved. You will not be asked this again.', 'ok');
      card.remove();
      onGone();
    } catch (e) {
      saveBtn.disabled = false;
      dropBtn.disabled = false;
      fail(e.message);
    }
  };

  dropBtn.onclick = async () => {
    const ok = await confirmDialog('Dismiss this question?',
      'Dismissing does not answer it. The field is simply left for you to fill by hand '
      + 'next time it comes up, and the question will not be raised again here.',
      'Dismiss');
    if (!ok) return;
    saveBtn.disabled = true;
    dropBtn.disabled = true;
    try {
      await del(`/api/asks/${a.id}`);
      toast('Dismissed.', 'info');
      card.remove();
      onGone();
    } catch (e) {
      saveBtn.disabled = false;
      dropBtn.disabled = false;
      fail(e.message);
    }
  };

  card.append(
    el('h2', {}, question),
    el('div', { class: 'row tight small' },
      el('span', { class: 'muted' }, where),
      el('span', { class: 'dim' }, `asked ${fmtAgo(a.created_at)}`),
      a.field_type ? pill(a.field_type, 'info') : null),
    a.context ? el('div', { class: 'small dim u-mt-6px' }, a.context) : null,
    el('div', { class: 'u-mt-10px' }, input),
    err,
    el('div', { class: 'row u-mt-10px' }, saveBtn, dropBtn),
  );
  return card;
}

function controlFor(a, question) {
  const options = (Array.isArray(a.options) ? a.options : [])
    .filter((o) => o !== null && o !== undefined && String(o) !== '');

  if (options.length) {
    // The blank sits first so nothing is submitted by accident just because a
    // select always has something selected.
    return el('select', {}, el('option', { value: '' }, '— choose —'),
      ...options.map((o) => el('option', { value: String(o) }, String(o))));
  }
  if (a.field_type === 'date') return el('input', { type: 'date' });
  if (question.length > 120) return el('textarea', { rows: 6 });
  return el('input', { type: 'text' });
}

function gapsCard(gaps) {
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Profile fields still set to ASK',
      el('span', { class: 'sub' }, 'these cause questions rather than answering them')),
    el('div', { class: 'small muted' },
      'Filling these in Settings resolves whole classes of question at once, '
      + 'and keeps legal answers out of the model\'s hands.'),
    el('div', { class: 'u-mt-8px' },
      ...gaps.map((g) => el('span', { class: 'chip' }, g))),
    el('div', { class: 'row u-mt-12px' },
      el('button', { class: 'btn', onclick: () => go('settings') }, 'Open Settings')),
  );
  return card;
}

/* ------------------------------------------------------- the answer bank -- */
async function renderBank(host, ctx) {
  const tableHost = el('div', {});
  const search = el('input', {
    type: 'text', placeholder: 'Search questions and answers…', autocomplete: 'off' });

  let timer = null;
  search.oninput = () => {
    // Debounced: the endpoint LIKE-scans the table, and a keystroke per query
    // makes the list flicker between stale results.
    clearTimeout(timer);
    timer = setTimeout(() => load(search.value.trim()), 220);
  };
  search.onkeydown = (e) => {
    if (e.key !== 'Enter') return;
    clearTimeout(timer);
    load(search.value.trim());
  };

  const card = el('div', { class: 'card' },
    el('h2', {}, 'Remembered answers',
      el('span', { class: 'sub' }, 'reused automatically on every form')),
    el('div', { class: 'filters u-mb-12px' }, search),
    tableHost);
  host.appendChild(card);

  await load('');

  async function load(q) {
    clear(tableHost).appendChild(el('div', { class: 'empty' }, 'Loading…'));
    let data;
    try {
      data = await get(`/api/answers${q ? `?q=${encodeURIComponent(q)}` : ''}`);
    } catch (e) {
      clear(tableHost).appendChild(el('div', { class: 'banner bad' }, e.message));
      return;
    }
    const rows = data.answers || [];
    clear(tableHost);

    if (!rows.length) {
      tableHost.appendChild(el('div', { class: 'empty' }, q
        ? `Nothing matches "${q}". Clear the search to see the whole bank.`
        : 'Nothing remembered yet. Answer a question on the first tab and it lands here.'));
      return;
    }

    const tb = el('tbody');
    for (const r of rows) tb.appendChild(answerRow(r, () => load(q)));
    tableHost.appendChild(el('div', { class: 'scroll-y' }, el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Question'),
        el('th', {}, 'Answer'),
        el('th', { class: 'shrink' }, 'Source'),
        el('th', { class: 'shrink' }, 'Locked'),
        el('th', { class: 'shrink' }, 'Used'),
        el('th', { class: 'shrink' }, ''))),
      tb)));
  }
}

const LEGAL_NOTE = 'Taken verbatim from your profile because it is immigration or '
  + 'identity relevant. Never model-generated, never fuzzy-matched.';

function answerRow(r, reload) {
  const legal = Number(r.is_legal) === 1;
  const tr = el('tr', legal ? { title: LEGAL_NOTE } : {});

  const field = el('input', { type: 'text', value: r.answer ?? '' });
  let saved = r.answer ?? '';
  let busy = false;

  field.onblur = async () => {
    const next = field.value;
    if (busy || next === saved) return;
    busy = true;
    try {
      if (legal) {
        const ok = await confirmDialog('Edit a legal answer?',
          'This answer appears on immigration-relevant forms — work authorisation, '
          + 'sponsorship, visa status. A wrong value here is a false statement on an '
          + 'application, not a typo. Change it only if the profile is wrong too.',
          'Change it');
        if (!ok) {
          field.value = saved;
          return;
        }
      }
      await patch(`/api/answers/${r.id}`, { answer: next });
      saved = next;
      toast('Answer updated.', 'ok');
    } catch (e) {
      field.value = saved;
      toast(e.message, 'bad');
    } finally {
      busy = false;
    }
  };

  const lock = el('input', { type: 'checkbox', checked: Number(r.locked) === 1 });
  lock.onchange = async () => {
    const want = lock.checked;
    try {
      await patch(`/api/answers/${r.id}`, { locked: want });
      toast(want ? 'Locked — nothing will overwrite it.' : 'Unlocked.', 'ok');
    } catch (e) {
      lock.checked = !want;
      toast(e.message, 'bad');
    }
  };

  const drop = el('button', { class: 'btn btn-sm btn-danger' }, 'Delete');
  drop.onclick = async () => {
    const ok = await confirmDialog('Delete this answer?',
      legal
        ? 'This is a legal answer. Deleting it means the next form asking it stops and '
        + 'waits for you. ' + (r.question_text || '')
        : 'The next form that asks this will queue it as a question again. '
        + (r.question_text || ''),
      'Delete');
    if (!ok) return;
    drop.disabled = true;
    try {
      await del(`/api/answers/${r.id}`);
      toast('Deleted.', 'ok');
      reload();
    } catch (e) {
      drop.disabled = false;
      toast(e.message, 'bad');
    }
  };

  tr.append(
    el('td', {},
      el('div', {}, r.question_text || '—'),
      el('div', { class: 'dim small' },
        r.last_used_at ? `last used ${fmtAgo(r.last_used_at)}` : 'not used yet')),
    el('td', {}, field),
    el('td', { class: 'shrink' },
      el('div', { class: 'row tight' },
        pill(r.source || 'unknown', r.source === 'llm' ? 'warn' : r.source === 'profile' ? 'ok' : ''),
        legal ? el('span', { class: 'pill info', title: LEGAL_NOTE }, 'legal') : null)),
    el('td', { class: 'shrink' }, lock),
    el('td', { class: 'num shrink' }, String(r.times_used ?? 0)),
    el('td', { class: 'shrink right' }, drop),
  );
  return tr;
}
