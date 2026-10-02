/* Popup: look at the tab you are on, hand it to applier, then get out of the way.
 *
 * Deliberately thin. It starts a task on the server and closes; the service
 * worker does the watching. Nothing here holds state that matters, because a
 * popup is destroyed the instant it loses focus — which is exactly what
 * happens when you switch tabs, the thing this extension exists to let you do.
 *
 * As in the web UI, every string from the server or the page goes in through
 * textContent. A job title comes from an employer's site and is not trusted.
 */

import { api, ping, settings } from './api.js';

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, '');
    else n.setAttribute(k, v);
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

let chosenLevel = null;
let currentUrl = '';

/* A posting URL is worth attempting; a search page is not. This only decides
 * whether to warn — the button stays usable either way, because the list of
 * places a job can live is open-ended and guessing wrong should not block you. */
function looksLikeAPosting(u) {
  try {
    const url = new URL(u);
    if (!/^https?:$/.test(url.protocol)) return false;
    const s = (url.hostname + url.pathname).toLowerCase();
    const atsHosts = /greenhouse|lever\.co|ashbyhq|workable|recruitee|smartrecruiters|icims|myworkdayjobs|teamtailor|bamboohr|jobvite|breezy|workatastartup/;
    const jobPath = /\/(job|jobs|careers|opening|openings|position|positions|vacancy|apply)\b/;
    return atsHosts.test(s) || jobPath.test(s);
  } catch { return false; }
}

async function boot() {
  const { token } = await settings();
  if (!token) return showUnpaired();

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = tab?.url || '';
  $('url').textContent = currentUrl || 'no page';

  if (currentUrl && !looksLikeAPosting(currentUrl)) {
    const n = $('not-a-job');
    n.textContent = 'This does not look like a job posting. You can still try — '
                  + 'applier reads whatever form is actually on the page.';
    n.hidden = false;
  }

  let state;
  try {
    state = await ping();
  } catch (e) {
    $('status').textContent = 'offline';
    $('status').className = 'status bad';
    const m = $('apply-msg');
    m.textContent = e.message;
    m.className = 'msg bad';
    m.hidden = false;
    renderTasks([]);
    return;
  }

  $('status').textContent = 'connected';
  $('status').className = 'status ok';
  renderLevels(state.autonomy);
  renderFunnel(state.funnel, state.open_asks);
  $('apply').disabled = !currentUrl;
  await refreshTasks();
}

function showUnpaired() {
  $('main').hidden = true;
  $('unpaired').hidden = false;
  $('status').textContent = 'not paired';
  $('open-options').onclick = () => chrome.runtime.openOptionsPage();
}

function renderLevels(autonomy) {
  const host = $('levels');
  host.replaceChildren();
  chosenLevel = autonomy.level;
  for (const l of autonomy.levels) {
    const row = el('label', { class: `level ${l.key === chosenLevel ? 'on' : ''}` },
      el('input', {
        type: 'radio', name: 'lvl', checked: l.key === chosenLevel,
        onchange: () => {
          chosenLevel = l.key;
          [...host.children].forEach((c, i) =>
            c.classList.toggle('on', autonomy.levels[i].key === chosenLevel));
        },
      }),
      el('div', {}, el('div', { class: 't' }, l.label),
                    el('div', { class: 'd' }, l.blurb)),
      el('span', { class: `tag ${l.submits ? 'submits' : 'safe'}` },
         l.submits ? 'submits' : 'never submits'),
    );
    host.appendChild(row);
  }
}

function renderFunnel(f, openAsks) {
  const bits = [`${f.queued} queued`, `${f.submitted} submitted`];
  if (openAsks) bits.push(`${openAsks} question${openAsks > 1 ? 's' : ''} waiting`);
  $('funnel').textContent = bits.join('  ·  ');
}

function renderTasks(tasks) {
  const host = $('tasks');
  host.replaceChildren();
  const show = tasks.slice(0, 5);
  if (!show.length) {
    host.appendChild(el('div', { class: 'empty' }, 'Nothing running.'));
    return;
  }
  for (const t of show) {
    host.appendChild(el('div', { class: 'task' },
      el('div', { class: 'n' }, t.name),
      el('div', { class: 'm' },
        el('span', { class: `st-${t.status}` }, t.status),
        `  ${Math.round(t.elapsed)}s`,
        t.result ? `  — ${String(t.result).slice(0, 48)}` : '',
        t.error ? `  — ${String(t.error).slice(0, 48)}` : ''),
    ));
  }
}

async function refreshTasks() {
  try {
    const { tasks } = await api('/api/tasks');
    renderTasks(tasks);
  } catch {
    renderTasks([]);
  }
}

$('apply').onclick = async () => {
  const btn = $('apply');
  const msg = $('apply-msg');
  btn.disabled = true;
  btn.textContent = 'Starting…';
  msg.hidden = true;
  try {
    const { task } = await api('/api/apply', {
      method: 'POST',
      body: { url: currentUrl, level: chosenLevel },
    });
    msg.textContent = `Started. It runs on your machine — close this and carry on; `
                    + `you will get a notification when it finishes.`;
    msg.className = 'msg ok';
    msg.hidden = false;
    btn.textContent = 'Running in the background';
    chrome.runtime.sendMessage({ type: 'poll-now' });
    void task;
    await refreshTasks();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'msg bad';
    msg.hidden = false;
    btn.disabled = false;
    btn.textContent = 'Apply to this posting';
  }
};

$('refresh').onclick = refreshTasks;

$('open-dash').onclick = async () => {
  const { port, token } = await settings();
  chrome.tabs.create({ url: `http://127.0.0.1:${port}/#t=${token}` });
};

boot();
