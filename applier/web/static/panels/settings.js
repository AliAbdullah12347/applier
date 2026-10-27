/* Settings — the autonomy dial, the key, the caps, and the answers that go
 * onto forms verbatim.
 *
 * Every control here is backed by an allow-list on the server (WRITABLE for
 * settings, PROFILE_WRITABLE for the profile), so this panel deliberately
 * offers nothing outside them: a field the API will refuse looks like it
 * saved and then quietly did not. The profile tab is the sober one — those
 * values are copied onto immigration-relevant documents exactly as typed.
 */

import {
  el, clear, get, post, put, pill, toast, modal, refreshChrome,
  token } from '../app.js';

const THEME_KEY = 'applier.theme';

/* Applied at import rather than inside render(): the shell hard-codes dark in
 * index.html and never reads the stored choice, so the earliest moment this
 * module can honour it is when it loads. */
applyTheme(readTheme());

/* Survives ctx.refresh() so that saving a group does not bounce you back to
 * the first tab. */
let activeTab = 'autonomy';

export async function render(ctx) {
  ctx.actions.append(
    el('button', { class: 'btn', onclick: () => ctx.refresh() }, 'Reload'),
  );

  const body = el('div', {});
  const tabs = el('div', { class: 'tabs' });
  const spec = [
    ['autonomy', 'Autonomy', renderAutonomy],
    ['model', 'Model and keys', renderModel],
    ['limits', 'Limits', renderLimits],
    ['profile', 'Profile', renderProfile],
    ['maintenance', 'Maintenance', renderMaintenance],
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

/* --------------------------------------------------------------- shared -- */
function loading(host, what) {
  clear(host).appendChild(el('div', { class: 'empty' }, what));
}

function failed(host, e) {
  clear(host).appendChild(el('div', { class: 'banner bad' }, e.message));
}

/** Read a dotted path out of a nested object, including list indices.
 *
 * `education[0].institution` is the shape the server reports an unanswered
 * field in, so it has to be the shape this can read one back out of --
 * otherwise the input renders empty and saving it looks like a change when
 * nothing changed. */
function dig(obj, path) {
  return path.split('.').reduce((o, seg) => {
    if (o === null || o === undefined) return o;
    const m = /^(.*?)\[(\d+)\]$/.exec(seg);
    if (!m) return o[seg];
    const list = o[m[1]];
    return Array.isArray(list) ? list[Number(m[2])] : undefined;
  }, obj);
}

/* GET /api/settings runs its payload through redact(), which masks any value
 * whose KEY NAME looks secret — api_key_env is masked to "GEM***" even though
 * it holds nothing but the name of an environment variable. Writing that back
 * would replace a working variable name with three asterisks, so a value that
 * still carries the mask is never sent. */
const masked = (v) => typeof v === 'string' && v.endsWith('***');

async function saveSetting(key, value) {
  if (masked(value)) throw new Error('still the redacted placeholder — type the value in full');
  await put('/api/settings', { updates: { [key]: value } });
}

/** A labelled text input that writes one settings key when it loses focus. */
function textField(label, key, value, hint) {
  let last = value === null || value === undefined ? '' : String(value);
  const input = el('input', { type: 'text', value: last, spellcheck: 'false' });
  input.addEventListener('change', async () => {
    const next = input.value.trim();
    try {
      await saveSetting(key, next);
      last = next;
      toast(`${label} saved`, 'ok');
    } catch (e) {
      input.value = last;
      toast(`${label}: ${e.message}`, 'bad');
    }
  });
  return el('label', { class: 'field' },
    el('span', {}, label, ' ', el('span', { class: 'mono dim' }, key)),
    input,
    hint ? el('div', { class: 'dim small u-mt-4px' }, hint) : null);
}

/* Validated on this side as well as the server's: put_settings casts with
 * int()/float() and an empty box comes back as a 400 that says nothing useful
 * about which field it was. */
function numberField(label, key, value, opts = {}) {
  const { step = 1, min = null, max = null, hint = '' } = opts;
  const whole = step === 1;
  let last = value === null || value === undefined ? '' : String(value);
  const attrs = { type: 'number', value: last, step: String(step) };
  if (min !== null) attrs.min = String(min);
  if (max !== null) attrs.max = String(max);
  const input = el('input', attrs);

  input.addEventListener('change', async () => {
    const n = Number(input.value);
    const range = min !== null && max !== null ? ` between ${min} and ${max}`
      : min !== null ? ` of at least ${min}` : '';
    if (input.value.trim() === '' || !Number.isFinite(n)
        || (whole && !Number.isInteger(n))
        || (min !== null && n < min) || (max !== null && n > max)) {
      input.value = last;
      toast(`${label}: needs ${whole ? 'a whole number' : 'a number'}${range}`, 'bad');
      return;
    }
    try {
      await saveSetting(key, n);
      last = String(n);
      toast(`${label} saved`, 'ok');
    } catch (e) {
      input.value = last;
      toast(`${label}: ${e.message}`, 'bad');
    }
  });

  return el('label', { class: 'field' },
    el('span', {}, label, ' ', el('span', { class: 'mono dim' }, key)),
    input,
    hint ? el('div', { class: 'dim small u-mt-4px' }, hint) : null);
}

function checkField(label, key, checked, hint) {
  const box = el('input', { type: 'checkbox', checked: Boolean(checked) });
  box.addEventListener('change', async () => {
    try {
      await saveSetting(key, box.checked);
      toast(`${label}: ${box.checked ? 'on' : 'off'}`, 'ok');
    } catch (e) {
      box.checked = !box.checked;
      toast(`${label}: ${e.message}`, 'bad');
    }
  });
  return el('div', { class: 'u-mb-11px' },
    el('label', { class: 'inline u-mb-2px' }, box, label),
    hint ? el('div', { class: 'dim small u-ml-22px' }, hint) : null);
}

/* ------------------------------------------------------------- autonomy -- */
/* Two different questions that both sound like "mode".
 *
 * This one is WHAT you are looking for -- internships or jobs. The dial below
 * is HOW MUCH happens without you. They are adjacent because people reach for
 * them in the same moment, and labelled plainly because conflating them means
 * either applying to the wrong kind of posting or submitting when you meant
 * to review. */
async function searchModeCard(ctx) {
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Looking for',
      el('span', { class: 'sub' }, 'internships, or jobs')));

  let data;
  try {
    data = await get('/api/mode');
  } catch (e) {
    card.appendChild(el('div', { class: 'banner bad' }, e.message));
    return card;
  }

  const choices = el('div', {});
  for (const m of data.modes) {
    const on = m.key === data.mode;
    const row = el('label', { class: `pick${on ? ' on' : ''}` },
      el('input', {
        type: 'radio', name: 'search-mode', value: m.key, checked: on || null,
        onchange: async () => {
          try {
            const next = await post('/api/mode', { mode: m.key });
            toast(`Now looking for ${next.mode === 'job' ? 'jobs' : 'internships'}`, 'ok');
            if (ctx && ctx.refresh) ctx.refresh();
          } catch (err) {
            toast(err.message, 'bad');
          }
        },
      }),
      el('div', { class: 'u-c3-2' },
        el('div', { class: 'u-c2-2' }, m.label,
          ' ', pill(`${m.postings} posting${m.postings === 1 ? '' : 's'}`,
                    m.postings ? 'info' : 'warn')),
        el('div', { class: 'dim small' }, m.blurb)));
    choices.appendChild(row);
  }
  card.appendChild(choices);
  card.appendChild(el('p', { class: 'muted small u-mt-10px' },
    'Postings of the other kind are set aside, not deleted — switching brings '
    + 'them straight back. Run a rank afterwards to refill the queue.'));
  return card;
}

async function renderAutonomy(host, ctx) {
  loading(host, 'Loading the current mode…');
  let s;
  try { s = await get('/api/settings'); } catch (e) { failed(host, e); return; }
  clear(host);

  const a = s.autonomy || {};
  const levels = a.levels || [];
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Autonomy', el('span', { class: 'sub' }, 'how much it does without you')));

  if (!levels.length) {
    card.appendChild(el('div', { class: 'empty' },
      'The server returned no autonomy levels. Run the system check on the "Model and keys" tab — settings.yaml may not have loaded.'));
  } else {
    card.appendChild(el('div', { class: 'levels' },
      ...levels.map((l) => el('label', { class: `level ${l.key === a.level ? 'on' : ''}` },
        el('input', {
          type: 'radio', name: 'autonomy', checked: l.key === a.level,
          onchange: async () => {
            try {
              const r = await post('/api/autonomy', { level: l.key });
              toast(`Mode: ${r.label}`, 'ok');
              await refreshChrome();  // the sidebar carries the mode; it should not lag
              ctx.refresh();
            } catch (e) { toast(e.message, 'bad'); }
          } }),
        el('div', {},
          el('div', { class: 'lv-label' }, l.label),
          el('div', { class: 'lv-blurb' }, l.blurb)),
        l.submits ? pill('submits', 'warn') : pill('never submits', 'ok'),
      ))));
  }

  const note = el('div', { class: 'card' },
    el('h2', {}, 'What the level does not change'),
    el('p', { class: 'dim small u-m-0' },
      'The eligibility gate still runs, and a role you cannot lawfully take is still rejected before '
      + 'anything is written. A legal or work-authorisation question that cannot be resolved from your '
      + 'profile still halts that application instead of being guessed at. A visible CAPTCHA still '
      + 'pauses and waits for you. The per-day, per-hour and per-employer caps hold at every level. '
      + 'The dial moves how much happens unattended — not what the system is allowed to do.'),
    el('p', { class: 'dim small u-m-8px00' },
      'Changing the level also rewrites apply.mode and the auto-apply score threshold on the Limits tab, '
      + 'because those two are what the level actually means.'));

  host.append(await searchModeCard(ctx), card, note);
}

/* -------------------------------------------------------- model and keys -- */
async function renderModel(host, ctx) {
  loading(host, 'Loading settings and key status…');
  let s;
  let sec;
  try {
    [s, sec] = await Promise.all([get('/api/settings'), get('/api/secrets')]);
  } catch (e) { failed(host, e); return; }
  clear(host);

  const v = (k) => dig(s.settings, k);

  /* ------------------------------------------------------------- secrets */
  const secCard = el('div', { class: 'card' },
    el('h2', {}, 'API keys', el('span', { class: 'sub' }, 'held in the OS keychain, not in settings.yaml')));
  const rows = sec.secrets || [];
  if (!rows.length) {
    secCard.appendChild(el('div', { class: 'empty' },
      'No key names to show. Set llm.primary.api_key_env below, then reload this tab.'));
  } else {
    const tb = el('tbody');
    for (const k of rows) {
      tb.appendChild(el('tr', {},
        el('td', { class: 'mono' }, k.name),
        el('td', { class: 'shrink' }, k.set ? pill('set', 'ok') : pill('not set', 'warn')),
        el('td', { class: 'shrink mono dim small' }, k.hint || '—'),
        el('td', { class: 'shrink right' },
          el('button', {
            class: 'btn btn-sm', onclick: () => setSecret(k.name, ctx) }, k.set ? 'Replace' : 'Set')),
      ));
    }
    secCard.appendChild(el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Key'),
        el('th', { class: 'shrink' }, 'Status'),
        el('th', { class: 'shrink' }, 'Last 4'),
        el('th', { class: 'shrink' }, ''))),
      tb));
  }
  secCard.appendChild(el('div', { class: 'dim small u-mt-10px' },
    'No screen can show you a stored key: there is no endpoint that returns one, only whether it is '
    + 'present and its last four characters. Whether a key actually works is a different question — a '
    + 'rejected key looks identical to a good one until something uses it, so the system check below '
    + 'makes a real call.'));

  /* --------------------------------------------------------------- model */
  const modelCard = el('div', { class: 'card' },
    el('h2', {}, 'Model', el('span', { class: 'sub' }, 'the primary link in detail; the whole chain is below')),
    el('div', { class: 'banner warn u-mb-14px' },
      el('span', {},
        'Some model names are listed by the provider and still return 404 when you call them. '
        + 'After changing the model, run the system check below — it is the only thing that proves it.')),
    textField('Provider', 'llm.primary.provider', v('llm.primary.provider'),
      'Lower case, as the router expects it: gemini, openai, anthropic.'),
    textField('Model', 'llm.primary.model', v('llm.primary.model')),
    textField('API key variable', 'llm.primary.api_key_env', v('llm.primary.api_key_env'),
      'The NAME of the environment variable or keychain entry, never the key itself. It is shown '
      + 'masked because its field name matches the redaction rule; type the whole name to change it, '
      + 'and the masked placeholder is never saved back.'),
    numberField('Temperature', 'llm.primary.temperature', v('llm.primary.temperature'),
      { step: 0.05, min: 0, max: 2, hint: 'Low for form filling. Cover letters do not improve above about 0.4.' }),
    numberField('Max output tokens', 'llm.primary.max_output_tokens', v('llm.primary.max_output_tokens'),
      { min: 256, hint: 'Thinking tokens are billed against this ceiling even though they never appear, '
        + 'so a tight value truncates the answer rather than the reasoning.' }),
    numberField('Thinking budget', 'llm.primary.thinking_budget', v('llm.primary.thinking_budget'),
      { min: 0 }),
  );

  host.append(secCard, modelCard, await chainCard(ctx), doctorCard());
}

function setSecret(name, ctx) {
  const input = el('input', { type: 'password', autocomplete: 'off', spellcheck: 'false' });
  const body = el('div', {},
    el('label', { class: 'field' }, el('span', {}, `Value for ${name}`), input),
    el('div', { class: 'dim small' },
      'This goes straight into the OS keychain. It is not written to settings.yaml, it is not echoed '
      + 'back by the API, and the event log records only that the key was set.'));

  modal(`Set ${name}`, body, [
    {
      label: 'Save',
      primary: true,
      onClick: async () => {
        const value = input.value;
        if (!value.trim()) { toast('Empty value — nothing saved', 'bad'); return true; }
        try {
          await put('/api/secrets', { name, value });
        } catch (e) {
          toast(e.message, 'bad');
          return true;
        }
        // The modal body stays in the DOM until the next modal replaces it, so
        // the field is wiped rather than left holding a key.
        input.value = '';
        toast(`${name} stored`, 'ok');
        ctx.refresh();
        return false;
      } },
  ]);
}

function doctorCard() {
  const body = el('div', {}, el('div', { class: 'empty' }, 'Not run yet this session.'));
  const btn = el('button', { class: 'btn' }, 'Run system check');

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    clear(body).appendChild(el('div', { class: 'empty' },
      'Checking… the live model call takes a few seconds.'));
    try {
      const r = await post('/api/doctor');
      clear(body);
      if (!r.checks?.length) {
        body.appendChild(el('div', { class: 'empty' }, 'The check returned nothing at all, which is itself a fault.'));
      }
      for (const c of r.checks || []) {
        body.appendChild(el('div', { class: `check ${c.ok ? 'ok' : 'bad'}` },
          el('span', { class: 'mark' }, c.ok ? '✓' : '✕'),
          el('div', {}, el('div', {}, c.name), el('div', { class: 'detail' }, c.detail))));
      }
      toast(r.ok ? 'All checks passed' : 'Some checks failed', r.ok ? 'ok' : 'bad');
    } catch (e) {
      clear(body).appendChild(el('div', { class: 'banner bad' }, e.message));
    } finally {
      btn.disabled = false;
    }
  });

  return el('div', { class: 'card' },
    el('h2', {}, 'System check', el('span', { class: 'sub' }, 'keys, dependencies, a real model call')),
    el('div', { class: 'row u-mb-10px' }, btn),
    body);
}

/* --------------------------------------------------------------- limits -- */
/* The whole model chain: primary, every fallback, and the per-task overrides.
 *
 * Only `llm.primary.model` used to be reachable from here, which meant that
 * the two things you reach for when a model is retired -- adding a fallback
 * and pointing one task somewhere else -- required opening a YAML file. That
 * is the moment a GUI stops being the way you run the system.
 *
 * Nothing on this screen is a secret. What is edited is the NAME of the
 * environment variable holding a key; the key itself is set in the card above
 * and lives in the OS keychain. */
async function chainCard(ctx) {
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Model chain',
      el('span', { class: 'sub' }, 'tried in order, top first')));

  let data;
  try {
    data = await get('/api/models');
  } catch (e) {
    card.appendChild(el('div', { class: 'banner bad' }, e.message));
    return card;
  }

  // Edited in memory and sent in one PUT, because a chain is an ordered list:
  // saving each row as you type would leave the chain briefly inconsistent.
  const state = {
    primary: { ...data.primary },
    fallbacks: data.fallbacks.map((f) => ({ ...f })),
    task_models: { ...data.task_models },
  };
  delete state.primary.api_key_set;
  state.fallbacks.forEach((f) => delete f.api_key_set);

  const body = el('div', {});

  const field = (spec, key, placeholder, width) => el('input', {
    class: `inp ${width || ''}`,
    value: spec[key] || '',
    placeholder,
    oninput: (e) => { spec[key] = e.target.value.trim(); },
  });

  const providerSelect = (spec) => {
    const sel = el('select', { class: 'inp u-minw-120px' });
    for (const p of data.providers) {
      sel.appendChild(el('option', { value: p, selected: spec.provider === p || null }, p));
    }
    sel.addEventListener('change', (e) => { spec.provider = e.target.value; draw(); });
    return sel;
  };

  function rowFor(spec, label, onRemove) {
    const needsUrl = spec.provider === 'openai_compatible';
    return el('tr', {},
      el('td', { class: 'shrink mono dim small' }, label),
      el('td', { class: 'shrink' }, providerSelect(spec)),
      el('td', {}, field(spec, 'model', 'model name')),
      el('td', { class: 'shrink' }, field(spec, 'api_key_env', 'KEY_ENV_VAR')),
      el('td', {}, field(spec, 'base_url',
        needsUrl ? 'https://… (required)' : 'optional override')),
      el('td', { class: 'shrink right' },
        onRemove
          ? el('button', { class: 'btn btn-sm', onclick: onRemove }, 'Remove')
          : pill('primary', 'ok')),
    );
  }

  function draw() {
    clear(body);

    const tb = el('tbody');
    tb.appendChild(rowFor(state.primary, 'primary', null));
    state.fallbacks.forEach((f, i) => {
      tb.appendChild(rowFor(f, String(i + 1), () => {
        state.fallbacks.splice(i, 1);
        draw();
      }));
    });

    body.appendChild(el('table', {},
      el('thead', {}, el('tr', {},
        el('th', { class: 'shrink' }, '#'),
        el('th', { class: 'shrink' }, 'Provider'),
        el('th', {}, 'Model'),
        el('th', { class: 'shrink' }, 'Key env'),
        el('th', {}, 'Endpoint'),
        el('th', { class: 'shrink' }, ''))),
      tb));

    body.appendChild(el('div', { class: 'row u-mt-10px' },
      el('button', {
        class: 'btn btn-sm',
        disabled: state.fallbacks.length >= data.max_fallbacks || null,
        onclick: () => {
          state.fallbacks.push({ ...state.primary });
          draw();
        },
      }, 'Add fallback'),
      el('span', { class: 'dim small' },
        `${state.fallbacks.length}/${data.max_fallbacks} — every link is tried `
        + 'before a task is allowed to fail.')));

    /* ------------------------------------------------- per-task overrides */
    const tasks = Object.keys(state.task_models);
    if (tasks.length) {
      body.appendChild(el('h3', { class: 'u-mt-16px u-mb-8px' }, 'Per-task model'));
      const tt = el('tbody');
      for (const t of tasks) {
        tt.appendChild(el('tr', {},
          el('td', { class: 'shrink mono small' }, t),
          el('td', {}, el('input', {
            class: 'inp',
            value: state.task_models[t] || 'primary',
            placeholder: 'primary',
            oninput: (e) => {
              state.task_models[t] = e.target.value.trim() || 'primary';
            },
          }))));
      }
      body.appendChild(el('table', {}, tt));
      body.appendChild(el('div', { class: 'dim small u-mt-8px' },
        "'primary' uses the chain above. Any other value overrides just the "
        + 'model name for that task, keeping the same provider and key.'));
    }

    body.appendChild(el('div', { class: 'row u-mt-12px' },
      el('button', {
        class: 'btn btn-primary',
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            await put('/api/models', state);
            toast('Model chain saved', 'ok');
            if (ctx && ctx.refresh) ctx.refresh();
          } catch (err) {
            toast(err.message, 'bad');
            e.target.disabled = false;
          }
        },
      }, 'Save chain'),
      el('span', { class: 'dim small' },
        'Checked before anything is written — an unknown provider or a '
        + 'missing endpoint is refused with the reason.')));

    body.appendChild(el('p', { class: 'muted small u-mt-12px' },
      'Any vendor exposing an OpenAI-compatible /chat/completions works '
      + "without a code change: choose provider 'openai_compatible' and give "
      + 'its endpoint. That is what keeps this working when today\'s models '
      + 'are retired.'));
  }

  draw();
  card.appendChild(body);
  return card;
}

async function renderLimits(host) {
  loading(host, 'Loading the caps…');
  let s;
  try { s = await get('/api/settings'); } catch (e) { failed(host, e); return; }
  clear(host);

  const v = (k) => dig(s.settings, k);

  host.append(
    el('div', { class: 'card' },
      el('h2', {}, 'How much it applies', el('span', { class: 'sub' }, 'volume caps, enforced at every autonomy level')),
      el('div', { class: 'grid c2' },
        numberField('Applications per day', 'apply.max_per_day', v('apply.max_per_day'), { min: 0 }),
        numberField('Applications per hour', 'apply.max_per_hour', v('apply.max_per_hour'), { min: 0 })),
      numberField('Score needed to apply unattended', 'search.min_score_to_autoapply',
        v('search.min_score_to_autoapply'),
        { step: 0.05, min: 0, max: 1, hint: 'A job below this is queued for you to look at instead. '
          + 'Moving the autonomy dial overwrites this number.' }),
      numberField('Discovery poll interval (minutes)', 'discovery.poll_interval_minutes',
        v('discovery.poll_interval_minutes'),
        { min: 1, hint: 'Board listings do not change minute to minute, and a tight interval mostly buys rate limits.' })),

    el('div', { class: 'card' },
      el('h2', {}, 'The form filler', el('span', { class: 'sub' }, 'what it will answer on its own')),
      numberField('Confidence threshold', 'apply.universal_filler.confidence_threshold',
        v('apply.universal_filler.confidence_threshold'),
        { step: 0.05, min: 0, max: 1, hint: 'Below this the field becomes a question for you rather than a guess on a form.' }),
      checkField('Universal filler enabled', 'apply.universal_filler.enabled',
        v('apply.universal_filler.enabled'),
        'Off means only fields with an exact mapping are filled; everything else waits for you.'),
      checkField('Create employer accounts automatically', 'apply.accounts.auto_create',
        v('apply.accounts.auto_create'),
        'Workday and similar portals need an account before the form exists.')),

    el('div', { class: 'card' },
      el('h2', {}, 'The browser'),
      checkField('Run headless', 'apply.headless', v('apply.headless'),
        'Leaving this off is deliberate. Several ATS fraud checks flag a headless browser, and a '
        + 'flagged session can sink the application rather than just fail it. A visible window also '
        + 'lets you see a CAPTCHA the moment it appears.'),
      checkField('Screenshot every step', 'apply.screenshot_every_step', v('apply.screenshot_every_step'),
        'Slower, and worth it the first time something submits the wrong answer: the screenshots are '
        + 'the only record of what the page actually looked like.')),
  );
}

/* -------------------------------------------------------------- profile -- */
const GROUP_ORDER = ['identity', 'address', 'work_authorization', 'education',
  'availability', 'languages', 'links', 'references', 'demographics', 'compensation'];
const GROUP_LABEL = {
  identity: 'Identity',
  address: 'Address',
  work_authorization: 'Work authorisation',
  education: 'Education',
  availability: 'Availability',
  languages: 'Languages',
  links: 'Links',
  references: 'References',
  demographics: 'Demographics',
  compensation: 'Compensation' };
/* Words that look wrong when they are merely capitalised. */
const WORDS = { us: 'US', uk: 'UK', eu: 'EU', usd: 'USD', linkedin: 'LinkedIn', github: 'GitHub' };

function prettify(seg) {
  return seg.split('_')
    .map((w) => WORDS[w] || (w.charAt(0).toUpperCase() + w.slice(1)).replace(/(\d+)$/, ' $1'))
    .join(' ');
}

/* The last segment alone is ambiguous — address.line1 and
 * address.permanent.line1 would both read "Line 1" — so the middle segments
 * stay in the label. */
function labelFor(path) {
  return path.split('.').slice(1).map((seg) => {
    const m = /^(.*?)\[(\d+)\]$/.exec(seg);
    // A second entry needs to be distinguishable from the first, but a lone
    // one should not be labelled "#1" for no reason.
    return m ? prettify(m[1]) + (Number(m[2]) ? ` #${Number(m[2]) + 1}` : '')
             : prettify(seg);
  }).join(' · ');
}

/** The group a field belongs to, ignoring any list index. */
function groupOf(path) {
  return path.split('.')[0].replace(/\[\d+\]$/, '');
}

async function renderProfile(host, ctx) {
  loading(host, 'Loading the profile…');
  let p;
  try { p = await get('/api/profile'); } catch (e) { failed(host, e); return; }
  clear(host);

  const writable = p.writable || [];
  if (!writable.length) {
    host.appendChild(el('div', { class: 'empty' },
      'The server returned no editable profile fields. Check that config/profile.yaml exists and parses, then reload.'));
    return;
  }

  if (p.gaps?.length) {
    host.appendChild(el('div', { class: 'banner warn' },
      el('span', {}, `${p.gaps.length} field${p.gaps.length > 1 ? 's' : ''} across the profile still say ASK. `
        + 'An application that needs one of them stops and waits, so the fastest way to unblock the queue is to fill these in.')));
  }

  const groups = new Map();
  for (const path of writable) {
    const g = groupOf(path);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(path);
  }
  const rank = (g) => (GROUP_ORDER.indexOf(g) < 0 ? 99 : GROUP_ORDER.indexOf(g));
  const ordered = [...groups.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));

  for (const g of ordered) {
    const paths = groups.get(g);
    const fields = el('div', {});
    const initial = new Map();
    const inputs = new Map();
    let asks = 0;

    for (const path of paths) {
      const raw = dig(p.profile, path);
      const val = raw === null || raw === undefined ? '' : String(raw);
      const isAsk = val.trim().toUpperCase() === 'ASK';
      if (isAsk) asks += 1;

      // Qualifier answers run to several sentences, and a single-line input
      // hides the end of a paragraph an employer will read in full.
      const long = val.length > 80 || path.endsWith('_qualifier');
      const input = long ? el('textarea', { rows: '3' }) : el('input', { type: 'text' });
      // Set as a property, not an attribute: a textarea has no value attribute.
      input.value = val;
      initial.set(path, val);
      inputs.set(path, input);

      fields.appendChild(el('label', { class: 'field' },
        el('span', {},
          labelFor(path), ' ',
          isAsk ? pill('unanswered', 'warn') : null, ' ',
          el('span', { class: 'mono dim' }, path)),
        input));
    }

    const save = el('button', { class: 'btn btn-primary' }, `Save ${GROUP_LABEL[g] || prettify(g)}`);
    save.addEventListener('click', async () => {
      const updates = {};
      for (const [path, input] of inputs) {
        const next = input.value.trim();
        if (next !== initial.get(path)) updates[path] = next;
      }
      const n = Object.keys(updates).length;
      if (!n) { toast('Nothing changed in this group', 'info'); return; }
      save.disabled = true;
      try {
        const r = await put('/api/profile', { updates });
        for (const k of Object.keys(updates)) initial.set(k, updates[k]);
        toast(`Saved ${r.changed?.length ?? n} field${(r.changed?.length ?? n) > 1 ? 's' : ''}`, 'ok');
        ctx.refresh();  // so the ASK pills and the gap banner catch up
      } catch (e) {
        toast(e.message, 'bad');
      } finally {
        save.disabled = false;
      }
    });

    const card = el('div', { class: 'card' },
      el('h2', {}, GROUP_LABEL[g] || prettify(g),
        el('span', { class: 'sub' }, `${paths.length} field${paths.length > 1 ? 's' : ''}`)));

    if (g === 'work_authorization') {
      card.appendChild(el('div', { class: 'banner bad u-mb-14px' },
        el('span', {},
          'These answers are used word for word on immigration-relevant forms, and no model writes or '
          + 'rewrites them. A mistake here does not stay in this file: it is carried onto a document an '
          + 'employer, and sometimes a government office, will read. Check each one against your I-20 '
          + 'or your DSO’s written guidance before you save.')));
    }
    if (asks) {
      card.appendChild(el('div', { class: 'banner warn u-mb-14px' },
        el('span', {}, `${asks} field${asks > 1 ? 's' : ''} here still says ASK.`)));
    }
    card.append(fields, el('div', { class: 'row' }, save));
    host.appendChild(card);
  }
}

/* ---------------------------------------------------------- maintenance -- */
/* The browser extension pairs by pasting this session's token. The token is
 * never returned by an endpoint — this page already has it, which is how it
 * loaded at all — so the pairing string is assembled here rather than served. */
async function extensionCard() {
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Browser extension',
      el('span', { class: 'sub' }, 'apply from the tab you are already on')));

  let info;
  try {
    ({ extension: info } = await get('/api/extension'));
  } catch (e) {
    card.appendChild(el('div', { class: 'banner bad' }, e.message));
    return card;
  }

  if (!info.installed) {
    card.appendChild(el('div', { class: 'banner warn' },
      `No extension found at ${info.path}. It ships with the project; if this is ` +
      `missing, the checkout is incomplete.`));
    return card;
  }

  const pairUrl = `${location.origin}/#t=${token()}`;

  const dl = el('dl', { class: 'kv' });
  dl.append(el('dt', {}, 'Load this folder'), el('dd', { class: 'mono small' }, info.path));
  dl.append(el('dt', {}, 'Version'), el('dd', { class: 'small' }, info.version || '—'));
  card.appendChild(dl);

  card.appendChild(el('p', { class: 'muted small u-mt-12px' },
    'In Chrome: open chrome://extensions, turn on Developer mode, choose ' +
    '"Load unpacked", and pick the folder above. Then click the extension, ' +
    'press "Pair with applier", and paste the link below.'));

  const box = el('pre', { class: 'jd mono' }, pairUrl);
  card.appendChild(box);

  card.appendChild(el('div', { class: 'row u-mt-8px' },
    el('button', {
      class: 'btn btn-primary',
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(pairUrl);
          toast('Pairing link copied', 'ok');
        } catch {
          // Clipboard access can be refused; selecting the text is as good.
          const r = document.createRange();
          r.selectNodeContents(box);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(r);
          toast('Select-all is ready — press Ctrl+C', 'info');
        }
      },
    }, 'Copy pairing link'),
    el('span', { class: 'dim small' },
      'This token dies when the server restarts, so pair again after a restart.')));

  return card;
}


/* Everything that must hold before leaving this running unattended.
 *
 * Deliberately a button rather than something that runs on load: it renders a
 * real PDF and makes a real model call, which costs a few seconds and a few
 * tokens. That is the right trade against a six-hour run that was never going
 * to work, but not against opening a settings tab. */
function preflightCard() {
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Preflight',
      el('span', { class: 'sub' }, 'before leaving it running unattended')));

  const results = el('div', {});
  const note = el('p', { class: 'muted small u-mt-8px' },
    'Renders a real resume, makes a real model call and starts a real browser '
    + '— the three things that look fine until something uses them. '
    + '`applier run` does this first and refuses to start on a blocking failure.');

  const button = el('button', { class: 'btn btn-primary' }, 'Run preflight');
  button.addEventListener('click', async () => {
    button.disabled = true;
    clear(results);
    results.appendChild(el('div', { class: 'dim small' }, 'Checking…'));
    try {
      const report = await post('/api/preflight', { deep: true });
      clear(results);
      const tb = el('tbody');
      for (const c of report.checks) {
        const tone = c.ok ? 'ok' : (c.blocking ? 'bad' : 'warn');
        const label = c.ok ? 'ok' : (c.blocking ? 'blocked' : 'warn');
        tb.appendChild(el('tr', {},
          el('td', { class: 'shrink' }, pill(label, tone)),
          el('td', { class: 'shrink' }, c.name),
          el('td', { class: 'small dim' }, c.detail || '—')));
        if (!c.ok && c.fix) {
          tb.appendChild(el('tr', {},
            el('td', {}),
            el('td', { colspan: '2', class: 'small' }, c.fix)));
        }
      }
      results.appendChild(el('table', {}, tb));
      results.appendChild(el('div', { class: report.ok ? 'banner ok' : 'banner bad' },
        report.ok
          ? 'Ready to run unattended.'
          : `Not ready — ${report.blocking.length} blocking problem(s): ${report.blocking.join(', ')}`));
    } catch (e) {
      clear(results);
      results.appendChild(el('div', { class: 'banner bad' }, e.message));
    }
    button.disabled = false;
  });

  card.append(el('div', { class: 'row' }, button), results, note);
  return card;
}

/* The three things here that cannot be rebuilt: the profile, the content
 * bank, and the record of what has been applied to. */
function backupCard() {
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Backup',
      el('span', { class: 'sub' }, 'the parts that cannot be regenerated')));

  const out = el('div', {});
  const button = el('button', { class: 'btn' }, 'Back up now');
  button.addEventListener('click', async () => {
    button.disabled = true;
    clear(out);
    out.appendChild(el('div', { class: 'dim small' }, 'Writing…'));
    try {
      const r = await post('/api/backup', {});
      clear(out);
      out.appendChild(el('div', { class: 'banner ok' },
        `${r.files} file(s), ${r.megabytes} MB`));
      out.appendChild(el('div', { class: 'mono small u-mt-8px' }, r.path));
    } catch (e) {
      clear(out);
      out.appendChild(el('div', { class: 'banner bad' }, e.message));
    }
    button.disabled = false;
  });

  card.append(el('div', { class: 'row' }, button), out,
    el('p', { class: 'muted small u-mt-8px' },
      'Your profile, your content bank, and the record of what you have '
      + 'applied to. Artifacts are left out — they regenerate from the bank. '
      + 'API keys are not included: they live in the OS keychain, and an '
      + 'archive containing them would be a plaintext secret in a folder.'));
  return card;
}

async function renderMaintenance(host) {
  loading(host, 'Loading paths…');
  let p;
  try { p = await get('/api/paths'); } catch (e) { failed(host, e); return; }
  clear(host);

  host.appendChild(await extensionCard());

  const entries = Object.entries(p.paths || {});
  const pathCard = el('div', { class: 'card' },
    el('h2', {}, 'Where things live', el('span', { class: 'sub' }, 'all local; nothing here is synced anywhere')));
  if (!entries.length) {
    pathCard.appendChild(el('div', { class: 'empty' },
      'The server reported no paths, which usually means the config failed to load. Run the system check on "Model and keys".'));
  } else {
    const dl = el('dl', { class: 'kv' });
    for (const [k, path] of entries) {
      dl.append(el('dt', {}, k), el('dd', { class: 'mono small' }, path));
    }
    pathCard.appendChild(dl);
  }

  /* --------------------------------------------------------- privacy audit */
  const out = el('div', {}, el('div', { class: 'empty' }, 'Not run yet this session.'));
  const auditBtn = el('button', { class: 'btn' }, 'Run privacy audit');
  auditBtn.addEventListener('click', async () => {
    auditBtn.disabled = true;
    clear(out).appendChild(el('div', { class: 'empty' }, 'Scanning the repository…'));
    try {
      const r = await post('/api/audit');
      clear(out).append(
        el('div', { class: `banner ${r.ok ? 'ok' : 'bad'}` },
          el('span', {}, r.ok
            ? 'Clean — nothing personal was found in anything that would be pushed.'
            : 'Findings below. Clear them before pushing; the same check runs there and will refuse.')),
        el('pre', { class: 'log u-c5-2' }, r.output || '(no output)'));
    } catch (e) {
      clear(out).appendChild(el('div', { class: 'banner bad' }, e.message));
    } finally {
      auditBtn.disabled = false;
    }
  });

  const auditCard = el('div', { class: 'card' },
    el('h2', {}, 'Privacy audit', el('span', { class: 'sub' }, 'personal data that must not leave this machine')),
    el('div', { class: 'dim small u-mb-10px' },
      'This is the same check that gates a push to GitHub, so a pass here is a push that will not be blocked.'),
    el('div', { class: 'row u-mb-10px' }, auditBtn),
    out);

  /* ----------------------------------------------------------------- theme */
  const current = readTheme();
  const sel = el('select', {},
    el('option', { value: 'dark', selected: current === 'dark' }, 'Dark'),
    el('option', { value: 'light', selected: current === 'light' }, 'Light'));
  sel.addEventListener('change', () => {
    applyTheme(sel.value);
    storeTheme(sel.value);
  });

  const themeCard = el('div', { class: 'card' },
    el('h2', {}, 'Appearance'),
    el('label', { class: 'field u-c6-2' },
      el('span', {}, 'Theme'), sel),
    el('div', { class: 'dim small u-mt-8px' },
      'Remembered in this browser. The page opens dark and switches once this panel loads, so the '
      + 'choice shows a moment late on a cold start.'));

  host.append(preflightCard(), backupCard(), pathCard, auditCard, themeCard);
}

function readTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch { /* storage can be blocked outright; the HTML default stands */ }
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

/* Normalised to the two known values rather than passed through: the stored
 * string ends up in a DOM attribute, and anything that reaches the DOM from
 * storage is treated as untrusted here like everything else. */
function applyTheme(name) {
  document.documentElement.dataset.theme = name === 'light' ? 'light' : 'dark';
}

function storeTheme(name) {
  try {
    localStorage.setItem(THEME_KEY, name === 'light' ? 'light' : 'dark');
  } catch {
    toast('Theme changed for this session only — this browser is blocking local storage', 'info');
  }
}
