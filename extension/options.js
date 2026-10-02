/* Pairing. Saves the token and port, then proves they work before saying so —
 * "saved" on its own is not information, because the common failure is a token
 * that is simply out of date. */

import { saveSettings, settings } from './api.js';

const $ = (id) => document.getElementById(id);

(async () => {
  const s = await settings();
  $('token').value = s.token || '';
  $('port').value = s.port || 8765;
})();

/* People paste the whole link as often as the token alone. Accept either. */
function extractToken(raw) {
  const v = (raw || '').trim();
  const m = /[#&]t=([\w-]+)/.exec(v);
  if (m) return m[1];
  return v.replace(/^#/, '');
}

$('save').onclick = async () => {
  const result = $('result');
  const token = extractToken($('token').value);
  const port = Number($('port').value) || 8765;

  if (!token) {
    result.textContent = 'paste the token first';
    result.className = 'status bad';
    return;
  }
  $('token').value = token;      // show what was actually stored

  result.textContent = 'testing…';
  result.className = 'status';
  await saveSettings({ token, port });

  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/state`, {
      headers: { 'X-Applier-Token': token },
      cache: 'no-store',
      credentials: 'omit',
    });
    if (res.status === 403) {
      result.textContent = 'refused — wrong token, or the server restarted since you copied it';
      result.className = 'status bad';
      return;
    }
    if (!res.ok) {
      result.textContent = `server replied ${res.status}`;
      result.className = 'status bad';
      return;
    }
    const state = await res.json();
    result.textContent = `connected — ${state.funnel.discovered} postings, `
                       + `${state.funnel.queued} queued`;
    result.className = 'status ok';
  } catch {
    result.textContent = 'cannot reach applier — is `applier gui` running?';
    result.className = 'status bad';
  }
};
