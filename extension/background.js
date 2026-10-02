/* Service worker: watches running applier tasks and reports when they end.
 *
 * The point of the extension is that you can start an application and then go
 * and do something else. That only works if something keeps watching after the
 * popup closes — a popup is destroyed the moment it loses focus, so the watch
 * cannot live there.
 *
 * MV3 service workers are also killed when idle, so the watch is driven by
 * chrome.alarms rather than setInterval: an alarm wakes the worker back up.
 * Task state lives in chrome.storage, not in a module variable, for the same
 * reason — a variable does not survive the worker being torn down.
 */

import { api } from './api.js';

const ALARM = 'applier-poll';
const SEEN_KEY = 'seenTaskStates';

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: 0.25 });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: 0.25 });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) poll();
});

/* The popup asks for an immediate poll after it starts something, so the
 * badge updates without waiting up to 15 seconds for the next alarm. */
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'poll-now') {
    poll().then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false }));
    return true;      // keep the channel open for the async reply
  }
  return false;
});

async function poll() {
  let tasks;
  try {
    ({ tasks } = await api('/api/tasks', { timeoutMs: 8000 }));
  } catch {
    // Not paired, or the server is not running. Both are normal; clear the
    // badge rather than nagging.
    await setBadge(0);
    return;
  }

  const running = tasks.filter((t) => t.status === 'running');
  await setBadge(running.length);

  const { [SEEN_KEY]: seen = {} } = await chrome.storage.local.get(SEEN_KEY);
  const next = {};
  for (const t of tasks) {
    next[t.id] = t.status;
    const before = seen[t.id];
    // Notify only on the transition into a terminal state, and only for a
    // task we actually watched start. Announcing tasks that were already
    // finished when the extension woke up would fire a burst of stale alerts.
    if (before === 'running' && t.status !== 'running') {
      notifyFinished(t);
    }
  }
  await chrome.storage.local.set({ [SEEN_KEY]: next });
}

function notifyFinished(task) {
  const ok = task.status === 'done';
  const body = task.error
    ? String(task.error).slice(0, 180)
    : (task.result ? String(task.result).slice(0, 180) : task.status);
  chrome.notifications.create(`applier-${task.id}`, {
    type: 'basic',
    iconUrl: 'icon128.png',
    title: ok ? `applier finished: ${task.name}`.slice(0, 90)
              : `applier ${task.status}: ${task.name}`.slice(0, 90),
    message: body,
    priority: ok ? 0 : 2,
  });
}

async function setBadge(n) {
  await chrome.action.setBadgeText({ text: n ? String(n) : '' });
  await chrome.action.setBadgeBackgroundColor({ color: '#4c9aff' });
}

// Opening the dashboard is more useful than dismissing the notification.
chrome.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith('applier-')) return;
  const { port = 8765, token = '' } = await chrome.storage.local.get(['port', 'token']);
  if (token) chrome.tabs.create({ url: `http://127.0.0.1:${port}/#t=${token}` });
  chrome.notifications.clear(id);
});
