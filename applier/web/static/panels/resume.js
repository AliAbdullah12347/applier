/* Resume — the master file, the bank it compiles into, and what a job picks.
 *
 * The thing people get wrong about this page: nothing here writes prose. The
 * bank holds phrasings Ali has already written, and tailoring is a selection
 * over them. So the useful question is never "what did it say" but "what did
 * it choose, and what did it have nothing to choose from" — which is why the
 * gap list on the preview tab is the most actionable thing on the screen.
 */

import {
  el, clear, get, post, put, pill, scoreCell, toast } from '../app.js';

function stat(n, label, kind) {
  return el('div', { class: `stat ${kind || ''}` },
    el('div', { class: 'n' }, String(n ?? 0)),
    el('div', { class: 'k' }, label));
}

const FORMAT = `## Experience
### Research Assistant — Computational Algebra   [pin]
org: Example University
dates: May 2026 -- Present
tags: algorithms, python, research

- A bullet, written in full, numbers inline   [pin]
  ~ medium variant, used when space is tight
  ~~ short variant, the last resort`;

export async function render(ctx) {
  ctx.view.appendChild(el('div', { class: 'empty' }, 'Loading the content bank…'));

  let bank;
  try {
    bank = await get('/api/bank');
  } catch (e) {
    clear(ctx.view).appendChild(el('div', { class: 'banner bad' },
      `Could not read the content bank: ${e.message}`));
    return;
  }
  clear(ctx.view);

  const intro = el('p', { class: 'muted u-m-0' },
    'One markdown file holds every experience and several phrasings of every bullet; '
    + 'the system never generates resume prose, it selects from what you have already '
    + 'written, and "rebuild" is what recompiles that file into the bank it selects from.');

  const tabMaster = el('button', { class: 'on', onclick: () => showTab(0) }, 'Master resume');
  const tabPreview = el('button', { onclick: () => showTab(1) }, 'Tailoring preview');
  const tabs = el('div', { class: 'tabs' }, tabMaster, tabPreview);

  const masterPane = el('div', { class: 'grid' });
  const previewPane = el('div', { class: 'grid' });
  previewPane.hidden = true;

  ctx.view.append(intro, tabs, masterPane, previewPane);

  /* ------------------------------------------------------------- master -- */
  let loadedText = bank.master || '';
  let dirty = false;
  let busy = false;

  const beforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; };

  function setDirty(v) {
    if (v === dirty) return;
    dirty = v;
    if (v) window.addEventListener('beforeunload', beforeUnload);
    else window.removeEventListener('beforeunload', beforeUnload);
    syncButtons();
  }

  // The shell navigates by emptying #view rather than replacing it, so the
  // only reliable signal that this panel is gone is our own nodes detaching.
  // Without this the unload warning would outlive the panel that owns it.
  const watcher = new MutationObserver(() => {
    if (!intro.isConnected) { setDirty(false); watcher.disconnect(); }
  });
  watcher.observe(ctx.view, { childList: true });

  const saveBtn = el('button', { class: 'btn btn-primary', onclick: () => run(doSave) }, 'Save');
  const saveRebuildBtn = el('button', {
    class: 'btn', onclick: () => run(async () => { await doSave(); await doRebuild(); }) }, 'Save and rebuild');
  const rebuildBtn = el('button', { class: 'btn', onclick: () => run(doRebuild) }, 'Rebuild only');
  ctx.actions.append(saveBtn, saveRebuildBtn, rebuildBtn);

  function syncButtons() {
    saveBtn.disabled = busy || !dirty;
    saveRebuildBtn.disabled = busy;
    rebuildBtn.disabled = busy;
  }

  async function run(fn) {
    busy = true;
    syncButtons();
    try {
      await fn();
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      busy = false;
      syncButtons();
    }
  }

  const statsRow = el('div', {
    class: 'grid u-gtc-repeatautofitminmax148px1fr' });
  const importNote = el('div', { class: 'grid' });
  const lintBox = el('div', {});

  const ta = el('textarea', {
    spellcheck: 'false',
    rows: '30',
    class: 'u-minheight-480px', 'aria-label': 'Master resume',
    oninput: () => setDirty(ta.value !== loadedText) });
  ta.value = loadedText;

  const editorCard = el('div', { class: 'card' },
    el('h2', {}, 'Master resume',
      el('span', { class: 'sub' }, 'everything you have ever done, no length limit')),
    ta,
    el('div', { class: 'dim small mono u-c4-2' },
      bank.path || '—'),
    bank.master_exists ? null : el('div', { class: 'banner warn u-mt-10px' },
      'Nothing saved yet — this is the bundled example. Replace it with your own and press Save.'));

  const formatCard = el('div', { class: 'card' },
    el('h2', {}, 'Format',
      el('span', { class: 'sub' }, '[pin] means non-negotiable; ~ lines are shorter versions')),
    el('pre', { class: 'jd' }, FORMAT));

  masterPane.append(statsRow, importNote, lintBox, editorCard, formatCard);

  renderStats(bank.stats);
  renderLint(bank.lint);
  syncButtons();

  function renderStats(s) {
    clear(statsRow);
    if (!s) {
      statsRow.appendChild(el('div', { class: 'banner warn' },
        'The bank has never been built from this file. Press "Rebuild only" to compile it.'));
      return;
    }
    statsRow.append(
      stat(s.atoms, 'Bullets'),
      stat(s.pinned, 'Pinned', s.pinned ? 'good' : 'warn'),
      stat(s.groups, 'Roles and projects'),
      stat(s.claims, 'Numbers tracked'),
      stat(s.retired, 'Retired', s.retired ? 'bad' : ''),
    );
  }

  function renderLint(problems) {
    clear(lintBox);
    const list = problems || [];
    if (!list.length) {
      lintBox.appendChild(el('div', { class: 'banner ok' },
        'No problems. Every phrasing renders and every number traces to a claim.'));
      return;
    }
    // A lint problem is not cosmetic: the renderer refuses to emit a phrasing
    // whose numbers it cannot trace, so each line here is a bullet that will
    // silently never appear on a tailored resume.
    const card = el('div', { class: 'card u-bordercolor-varbad' },
      el('h2', {}, `${list.length} problem${list.length > 1 ? 's' : ''}`,
        el('span', { class: 'sub' }, 'these phrasings cannot be rendered until fixed')));
    for (const p of list) {
      card.appendChild(el('div', { class: 'check bad' },
        el('span', { class: 'mark' }, '✕'),
        el('div', {}, el('div', { class: 'detail' }, String(p)))));
    }
    lintBox.appendChild(card);
  }

  async function doSave() {
    const text = ta.value;
    if (!text.trim()) throw new Error('The master resume cannot be empty.');
    const r = await put('/api/bank', { master: text });
    loadedText = text;
    setDirty(false);
    syncButtons();
    toast(`Saved, ${r.bytes} bytes. The previous version is kept as .md.bak.`, 'ok');
  }

  async function doRebuild() {
    const r = await post('/api/bank/import', {});
    clear(importNote);
    const bits = [`${r.atoms} bullets from ${r.entries} entries`, `${r.pinned} pinned`];
    if (r.skill_groups) bits.push(`${r.skill_groups} skill groups`);
    if (r.new_claims) bits.push(`${r.new_claims} new numbers`);
    if (r.retired_dropped) bits.push(`${r.retired_dropped} retired, kept out`);
    importNote.appendChild(el('div', { class: 'banner ok' }, `Rebuilt: ${bits.join(', ')}.`));
    if (r.needs_check?.length) {
      importNote.appendChild(el('div', { class: 'banner warn' },
        el('div', {},
          el('div', {}, `${r.needs_check.length} number${r.needs_check.length > 1 ? 's' : ''} `
            + 'arrived unconfirmed. They will render, but confirm each one in claims.yaml '
            + 'before an interviewer asks how you measured it.'),
          el('div', { class: 'row tight u-mt-6px' },
            ...r.needs_check.map((n) => el('span', { class: 'chip' }, String(n)))))));
    }
    renderLint(r.lint);
    // Counts are not in the import result in the same shape as the bank stats,
    // and re-reading is cheaper than reconciling two shapes.
    try {
      const fresh = await get('/api/bank');
      renderStats(fresh.stats);
    } catch { /* the rebuild itself succeeded; stale stats are not worth a red banner */ }
    toast('Bank rebuilt.', 'ok');
  }

  /* ------------------------------------------------------------ preview -- */
  const jobId = ctx.params?.id || null;

  const jdBox = el('textarea', {
    spellcheck: 'false',
    rows: '12',
    class: 'u-minheight-220px', placeholder: 'Paste the full job description here.',
    'aria-label': 'Job description' });
  const titleBox = el('input', {
    type: 'text', placeholder: 'e.g. Software Engineer Intern, Machine Learning' });
  const budgetBox = el('input', { type: 'number', min: '8', max: '90', step: '1', value: '38' });

  const previewBtn = el('button', {
    class: 'btn btn-primary',
    onclick: () => doPreview() }, 'Preview selection');

  const results = el('div', {});
  results.appendChild(el('div', { class: 'empty' },
    'Paste a job description and press "Preview selection" to see which of your own '
    + 'bullets it would put on the page, and which of its keywords you have nothing for.'));

  previewPane.append(
    el('div', { class: 'card' },
      el('h2', {}, 'What would this job get?',
        el('span', { class: 'sub' }, 'selection only — no wording is invented')),
      jobId ? el('div', { class: 'dim small u-mb-10px' },
        `Linked to job ${jobId}. Leave the box empty to use that posting's own description.`) : null,
      el('label', { class: 'field' }, el('span', {}, 'Job description'), jdBox),
      el('div', { class: 'grid c2' },
        el('label', { class: 'field' }, el('span', {}, 'Title (optional, helps detect the family)'), titleBox),
        el('label', { class: 'field' }, el('span', {}, 'Line budget'), budgetBox)),
      el('div', { class: 'row' }, previewBtn)),
    results);

  async function doPreview() {
    const description = jdBox.value.trim();
    if (!description && !jobId) {
      toast('Paste a job description first — a paragraph is enough.', 'bad');
      return;
    }
    const body = { description, budget: Number(budgetBox.value) || 38 };
    if (titleBox.value.trim()) body.title = titleBox.value.trim();
    if (jobId && !description) body.job_id = jobId;

    previewBtn.disabled = true;
    clear(results).appendChild(el('div', { class: 'empty' }, 'Selecting…'));
    try {
      renderPreview(await post('/api/bank/preview', body));
    } catch (e) {
      clear(results).appendChild(el('div', { class: 'banner bad' }, e.message));
    } finally {
      previewBtn.disabled = false;
    }
  }

  function renderPreview(r) {
    clear(results);
    const claimCount = Object.keys(r.claims_used || {}).length;

    results.appendChild(el('div', { class: 'card' },
      el('h2', {}, 'Selection',
        el('span', { class: 'sub' }, `${r.selected.length} bullets, budget ${r.budget} lines`)),
      el('div', { class: 'row' },
        el('span', { class: 'dim small' }, 'Family'), pill(r.family || 'default', 'info'),
        el('span', { class: 'dim small u-ml-10px' }, 'Coverage'), scoreCell(r.score),
        el('span', { class: 'dim small u-ml-10px' },
          `${claimCount} number${claimCount === 1 ? '' : 's'} traced to claims`))));

    if (r.pinned_dropped?.length) {
      results.appendChild(el('div', { class: 'banner warn' },
        el('div', {},
          el('div', {}, `${r.pinned_dropped.length} pinned bullet`
            + `${r.pinned_dropped.length > 1 ? 's' : ''} did not fit. A pin is a non-negotiable, `
            + 'so either raise the budget or unpin something.'),
          el('div', { class: 'row tight u-mt-6px' },
            ...r.pinned_dropped.map((id) => el('span', { class: 'chip' }, String(id))),
            el('span', { class: 'dim small' }, `${r.dropped} not selected in total`)))));
    } else if (r.dropped) {
      results.appendChild(el('div', { class: 'dim small' },
        `${r.dropped} other bullets in the bank were not selected for this one.`));
    }

    // Gaps first would bury the selection; gaps last would hide the only part
    // of this page that tells him what to go and build.
    const gapCard = el('div', { class: 'card' },
      el('h2', {}, 'Not covered by anything you have written',
        el('span', { class: 'sub' }, 'keywords in the posting with no matching bullet')));
    if (!r.gaps?.length) {
      gapCard.appendChild(el('div', { class: 'empty' },
        'Nothing missing. Every keyword this posting leans on has a bullet behind it.'));
    } else {
      gapCard.appendChild(el('div', { class: 'row tight' },
        ...r.gaps.map((g) => el('span', { class: 'chip' }, String(g)))));
    }
    results.appendChild(gapCard);

    const chosen = el('div', { class: 'card' },
      el('h2', {}, 'Chosen phrasings', el('span', { class: 'sub' }, 'grouped by role or project')));
    if (!r.selected.length) {
      chosen.appendChild(el('div', { class: 'empty' },
        'Nothing was selected. If the bank is empty, rebuild it from the master resume first.'));
    } else {
      const groups = new Map();
      for (const a of r.selected) {
        if (!groups.has(a.group)) groups.set(a.group, []);
        groups.get(a.group).push(a);
      }
      for (const [group, atoms] of groups) {
        chosen.append(
          el('div', { class: 'row u-mt-12px' },
            el('b', { class: 'small mono' }, group || '—'),
            el('span', { class: 'dim small' }, atoms[0].section || '')),
          ...atoms.map((a) => el('div', { class: `atom ${a.pinned ? 'pinned' : ''}` },
            el('div', {}, a.text || ''),
            el('div', { class: 'meta' },
              [a.id, a.size, (a.tags || []).join(' ')].filter(Boolean).join('  ·  ')))));
      }
    }
    results.appendChild(chosen);
  }

  /* --------------------------------------------------------------- tabs -- */
  function showTab(i) {
    tabMaster.classList.toggle('on', i === 0);
    tabPreview.classList.toggle('on', i === 1);
    masterPane.hidden = i !== 0;
    previewPane.hidden = i !== 1;
    // The save buttons act on the master file only; leaving them in the top
    // bar while the preview tab is open invites saving a file you cannot see.
    for (const b of [saveBtn, saveRebuildBtn, rebuildBtn]) b.hidden = i !== 0;
  }
}
