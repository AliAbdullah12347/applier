/* Shared client for the local applier server.
 *
 * Everything here talks to http://127.0.0.1:<port> and nowhere else. There is
 * no remote endpoint in this extension, no analytics, and no code loaded from
 * a network. If the applier server is not running, the extension does nothing
 * at all — it has no other capability.
 *
 * The session token is minted per launch by `applier gui` and pasted in once
 * on the options page. It lives in chrome.storage.local, which is readable
 * only by this extension.
 */

const DEFAULTS = { port: 8765, token: '' };

export async function settings() {
  const got = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...got };
}

export async function saveSettings(patch) {
  await chrome.storage.local.set(patch);
}

export function baseUrl(port) {
  return `http://127.0.0.1:${port}`;
}

/** A single API call. Throws Error(message) with the server's own wording. */
export async function api(path, { method = 'GET', body = null, timeoutMs = 20000 } = {}) {
  const { port, token } = await settings();
  if (!token) throw new Error('not paired — open the extension options and paste the token');

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(baseUrl(port) + path, {
      method,
      headers: {
        'X-Applier-Token': token,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : null,
      signal: ctl.signal,
      cache: 'no-store',
      credentials: 'omit',
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('the applier server did not respond');
    // A refused connection is the ordinary case: the server simply is not running.
    throw new Error('cannot reach applier — is `applier gui` running?');
  }
  clearTimeout(timer);

  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (res.status === 403) {
    throw new Error('refused — the token is wrong or the server restarted and minted a new one');
  }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

/** Connection probe used by the popup and the options page. */
export async function ping() {
  const state = await api('/api/state', { timeoutMs: 5000 });
  return state;
}
