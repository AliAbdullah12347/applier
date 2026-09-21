/* Outreach — find people, draft a message, then send it yourself.
 *
 * There is no send path anywhere in this codebase and this panel does not add
 * one. Everything stops at a draft on disk; the last mile is Ali's own mail
 * client, which is the only place a cold message to a stranger should come
 * from. The banner at the top says so on every render, deliberately.
 */

import {
  el, clear, get, post, runTask, pill, fmtDate, toast, link } from '../app.js';

const KINDS = [
  { key: 'github_org',  label: 'GitHub org',  placeholder: 'vercel' },
  { key: 'github_repo', label: 'GitHub repo', placeholder: 'owner/name' },
  { key: 'team_page',   label: 'Team page',   placeholder: 'https://company.com/team' },
];

const STAGES = ['harvested', 'drafted', 'sent', 'replied', 'declined', 'skipped'];

/* 'sent' gets its own colour rather than the generic ok: it is the one stage
   that only ever means "Ali pressed send somewhere else", and it should not
   look like something the tool did. */
const STAGE_KIND = {
  harvested: 'warn', drafted: 'info', sent: 'pin',
  replied: 'ok', declined: 'bad', skipped: 'bad' };

export async function render(ctx) {
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => ctx.refresh() }, 'Refresh'));

  ctx.view.appendChild(el('div', { class: 'banner ok' },
    el('span', {},
      'Drafts only. Nothing here is ever sent — you copy each message and send it yourself.')));

  ctx.view.appendChild(harvestCard(ctx));
  ctx.view.appendChild(draftCard(ctx));

  const listBody = el('div', {}, el('div', { class: 'empty' }, 'Loading contacts…'));
  ctx.view.appendChild(el('div', { class: 'card' },
    el('h2', {}, 'Contacts',
      el('span', { class: 'sub' }, 'the stage is yours to set — nothing moves it for you')),
    listBody));

  let data;
  try {
    data = await get('/api/outreach');
  } catch (e) {
    clear(listBody).appendChild(
      el('div', { class: 'banner bad' }, `Could not load contacts: ${e.message}`));
    return;
  }
  renderContacts(listBody, data.contacts || []);
}

/* ------------------------------------------------------------ find people -- */
function harvestCard(ctx) {
  const kindSel = el('select', {
    class: 'u-w-auto', onchange: () => { input.placeholder = placeholderFor(kindSel.value); } }, ...KINDS.map((k) => el('option', { value: k.key }, k.label)));

  const input = el('input', {
    type: 'text',
    placeholder: placeholderFor(KINDS[0].key),
    onkeydown: (e) => { if (e.key === 'Enter') harvest(); } });

  const btn = el('button', { class: 'btn btn-primary', onclick: harvest }, 'Harvest');

  async function harvest() {
    const kind = kindSel.value;
    const value = input.value.trim();
    if (!value) { toast('Type an org, a repo or a team-page URL first.', 'bad'); return; }
    // The server rejects a bare hostname anyway; catching it here saves a
    // round trip and a task that exists only to fail.
    if (kind === 'team_page' && !/^https?:\/\//i.test(value)) {
      toast('A team page needs the full https:// URL.', 'bad');
      return;
    }
    btn.disabled = true;
    try {
      await runTask('/api/outreach/harvest', { kind, value }, { onDone: ctx.refresh });
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      btn.disabled = false;
    }
  }

  return el('div', { class: 'card' },
    el('h2', {}, 'Find people',
      el('span', { class: 'sub' }, 'public sources only — an org, a repo, or a team page')),
    el('div', { class: 'row' },
      kindSel,
      el('div', { class: 'u-c3-2' }, input),
      btn));
}

function placeholderFor(key) {
  return (KINDS.find((k) => k.key === key) || KINDS[0]).placeholder;
}

/* --------------------------------------------------------- draft messages -- */
function draftCard(ctx) {
  const limit = el('input', {
    type: 'number', value: '4', min: '1', max: '25', class: 'u-w-84px' });
  const btn = el('button', { class: 'btn btn-primary', onclick: draft }, 'Draft');

  async function draft() {
    const n = Math.max(1, Math.min(Number(limit.value) || 4, 25));
    limit.value = String(n);
    btn.disabled = true;
    try {
      await runTask('/api/outreach/draft', { limit: n }, { onDone: ctx.refresh });
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      btn.disabled = false;
    }
  }

  return el('div', { class: 'card' },
    el('h2', {}, 'Draft messages',
      el('span', { class: 'sub' }, 'written to disk, never delivered')),
    el('div', { class: 'row' },
      el('span', { class: 'muted small' }, 'How many'),
      limit,
      btn),
    el('div', { class: 'dim small u-mt-8px' },
      'Drafting respects the weekly and per-company caps. A contact with no verifiable '
      + 'hook is dropped rather than padded with filler, so you may get fewer than you asked for.'));
}

/* ---------------------------------------------------------------- contacts -- */
function renderContacts(host, contacts) {
  clear(host);
  if (!contacts.length) {
    host.appendChild(el('div', { class: 'empty' },
      'No contacts yet. Harvest from a GitHub org or a team page above.'));
    return;
  }

  let stageFilter = 'all';
  let query = '';

  const stageSel = el('select', {
    onchange: (e) => { stageFilter = e.target.value; draw(); } }, el('option', { value: 'all' }, 'All stages'),
     ...STAGES.map((s) => el('option', { value: s }, s)));

  const search = el('input', {
    type: 'text', placeholder: 'Search name or company',
    oninput: (e) => { query = e.target.value.trim().toLowerCase(); draw(); } });

  const count = el('span', { class: 'dim small' });

  host.appendChild(el('div', { class: 'filters u-mb-10px' },
    stageSel, search, el('span', { class: 'spacer' }), count));

  const tbody = el('tbody');
  host.appendChild(el('div', { class: 'scroll-y' },
    el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Name'),
        el('th', {}, 'Role'),
        el('th', {}, 'Company'),
        el('th', {}, 'Channel'),
        el('th', { class: 'shrink' }, 'Stage'),
        el('th', { class: 'shrink' }, 'Draft'))),
      tbody)));

  draw();

  function draw() {
    clear(tbody);
    const shown = contacts.filter((c) =>
      (stageFilter === 'all' || c.stage === stageFilter)
      && (!query || `${c.name || ''} ${c.company || ''}`.toLowerCase().includes(query)));
    count.textContent = `${shown.length} of ${contacts.length} shown`;
    if (!shown.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: '6' },
        el('div', { class: 'empty' },
          'No contact matches that filter. Clear the search, or pick "All stages".'))));
      return;
    }
    for (const c of shown) tbody.appendChild(contactRow(c, tbody));
  }
}

function contactRow(c, tbody) {
  const hasDraft = typeof c.draft === 'string' && c.draft.trim().length > 0;

  let stagePill = pill(c.stage, STAGE_KIND[c.stage]);
  let settled = c.stage;

  const stageSel = el('select', { class: 'u-w-auto', onchange: onStage });
  for (const s of STAGES) stageSel.appendChild(el('option', { value: s }, s));
  stageSel.value = c.stage;

  async function onStage() {
    const next = stageSel.value;
    stageSel.disabled = true;
    try {
      await post(`/api/outreach/${c.id}/stage`, { stage: next });
      settled = next;
      // Swap the pill in place instead of re-rendering the table: an open
      // draft row underneath would otherwise close on every stage change.
      const fresh = pill(next, STAGE_KIND[next]);
      stagePill.replaceWith(fresh);
      stagePill = fresh;
      toast(next === 'sent'
        ? `Recorded: you sent to ${c.name || 'this contact'}.`
        : `${c.name || 'Contact'} → ${next}.`, 'ok');
    } catch (e) {
      stageSel.value = settled;
      toast(e.message, 'bad');
    } finally {
      stageSel.disabled = false;
    }
  }

  const nameCell = el('td', {}, el('div', {}, c.name || '(unnamed)'));
  if (!c.hook_fact) {
    nameCell.appendChild(el('div', { class: 'dim small' },
      'no hook found — drafting will skip this one'));
  }

  const channel = c.email
    ? el('td', { class: 'small mono' }, c.email)
    : el('td', { class: 'dim small' }, 'no email found');

  const toggle = el('button', { class: 'btn btn-sm' }, 'Show');
  const actions = el('td', { class: 'shrink' },
    hasDraft ? toggle : el('span', { class: 'dim small' }, '—'));

  const row = el('tr', {},
    nameCell,
    el('td', { class: 'small muted' }, c.role || '—'),
    el('td', {}, c.company || '—'),
    channel,
    el('td', { class: 'shrink' }, el('div', { class: 'row tight' }, stagePill, stageSel)),
    actions);

  if (hasDraft) {
    let open = null;
    toggle.onclick = () => {
      if (open) { open.remove(); open = null; toggle.textContent = 'Show'; return; }
      open = draftRow(c);
      tbody.insertBefore(open, row.nextSibling);
      toggle.textContent = 'Hide';
    };
  }

  return row;
}

function draftRow(c) {
  const pre = el('pre', { class: 'jd' }, c.draft);

  const meta = [];
  if (c.hook_fact) meta.push(el('div', { class: 'dim small' }, `Hook: ${c.hook_fact}`));
  // hook_source_url comes off an employer or GitHub page, so only an http(s)
  // value is ever put in an href — a javascript: URL must not become a link.
  if (c.hook_source_url) {
    meta.push(el('div', { class: 'small' },
      link(c.hook_source_url, 'Source page', { class: 'muted' })));
  }
  if (c.last_contact) {
    meta.push(el('div', { class: 'dim small' }, `You marked this sent ${fmtDate(c.last_contact)}.`));
  }

  const bar = el('div', { class: 'row u-mt-8px' },
    el('button', { class: 'btn btn-sm', onclick: () => copyDraft(c.draft, pre) }, 'Copy message'),
    el('span', { class: 'dim small' },
      'Read it before you send it. Nothing leaves this machine on its own.'));

  return el('tr', {}, el('td', { colspan: '6' },
    el('div', {}, pre, bar, ...meta)));
}

async function copyDraft(text, pre) {
  try {
    if (!navigator.clipboard) throw new Error('no clipboard API');
    await navigator.clipboard.writeText(text);
    toast('Copied. Paste it into your own mail client.', 'ok');
  } catch {
    // Clipboard access is refused outside a secure context and by some
    // policies; selecting the text leaves the user one keystroke away.
    try {
      const range = document.createRange();
      range.selectNodeContents(pre);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast('Clipboard blocked — the text is selected, press Ctrl+C.', 'info');
    } catch {
      toast('Could not copy. Select the text manually.', 'bad');
    }
  }
}
