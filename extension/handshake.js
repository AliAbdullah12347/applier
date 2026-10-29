/* Filling a Handshake application in the tab that is already signed in.
 *
 * Handshake is behind a university single sign-on. `applier apply <url>`
 * drives its own browser, and that browser is nobody: no session, so it
 * reaches a login page and stops. The browser that *is* signed in is this
 * one, so the filling happens here and the local server only prepares.
 *
 * What this does NOT do: submit. It fills, attaches the resume, reports
 * what it could not answer, and stops with the form on screen. Handshake
 * applications go to employers who can see the candidate's whole profile,
 * and a half-filled submission there costs more than a day's delay --
 * which is the same lesson the Ashby run taught the server side.
 *
 * Everything from the page is untrusted: a job description is written by an
 * employer. Nothing from it is ever interpolated into HTML, only set as
 * textContent, and nothing is evaluated.
 */

/* The same shape the server's own harvester produces, so the answers come
 * back keyed the way this page can use them. Kept deliberately generic
 * rather than keyed to Handshake's class names, which are generated and
 * change without notice; what is Handshake-specific is where the form is,
 * not what a form looks like. */
function visible(el) {
  if (!el || el.disabled) return false;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

function cssPath(el) {
  if (el.id) return `#${CSS.escape(el.id)}`;
  const name = el.getAttribute('name');
  if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && parts.length < 5) {
    let part = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const sibs = [...parent.children].filter((c) => c.tagName === node.tagName);
      if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(node) + 1})`;
    }
    parts.unshift(part);
    node = node.parentElement;
  }
  return parts.join(' > ');
}

function labelFor(el) {
  if (el.id) {
    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lab && lab.textContent.trim()) return lab.textContent.trim();
  }
  const wrap = el.closest('label');
  if (wrap && wrap.textContent.trim()) return wrap.textContent.trim();
  const aria = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby');
  if (aria) {
    const by = document.getElementById(aria);
    if (by && by.textContent.trim()) return by.textContent.trim();
    return aria;
  }
  // Handshake puts the question in a sibling above the control often enough
  // that this is worth one step up, and no further -- past that it starts
  // picking up section headings and calling them questions.
  const prev = el.closest('div')?.previousElementSibling;
  if (prev && prev.textContent.trim().length < 200) return prev.textContent.trim();
  return el.getAttribute('placeholder') || '';
}

function harvest() {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (!visible(el) && type !== 'file') return;
    const sel = cssPath(el);
    if (seen.has(sel)) return;
    seen.add(sel);

    let options = [];
    if (el.tagName === 'SELECT') {
      options = [...el.options].map((o) => o.textContent.trim()).filter(Boolean);
    } else if (type === 'radio' && el.name) {
      options = [...document.querySelectorAll(
        `input[type=radio][name="${CSS.escape(el.name)}"]`)]
        .map((r) => labelFor(r)).filter(Boolean);
    }
    out.push({
      selector: sel,
      tag: el.tagName.toLowerCase(),
      type,
      label: labelFor(el).replace(/\s+/g, ' ').trim().slice(0, 400),
      placeholder: el.getAttribute('placeholder') || '',
      required: el.required || el.getAttribute('aria-required') === 'true',
      value: (el.value !== undefined ? el.value : el.innerText) || '',
      options: options.slice(0, 60),
      maxlength: el.getAttribute('maxlength') || null,
    });
  });
  return out;
}

/* What the page says about the job. Handshake renders the posting beside
 * the form, so this reads the document rather than asking the server to
 * fetch a URL it cannot authenticate against. */
function posting() {
  const pick = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el && el.textContent.trim()) return el.textContent.trim().slice(0, 300);
    }
    return '';
  };
  const title = pick(['h1', '[data-hook="job-title"]', '[class*="job-title"]'])
    || document.title.split('|')[0].trim();
  const company = pick(['[data-hook="employer-name"]', '[class*="employer-name"]',
                        '[class*="company-name"]', 'h2']);
  // The description is the biggest block of prose on the page. Choosing it
  // by size rather than by selector is what survives a redesign.
  let best = '';
  document.querySelectorAll('div, section, article').forEach((el) => {
    if (el.querySelector('input, textarea, select')) return;
    const t = (el.innerText || '').trim();
    if (t.length > best.length && t.length < 40000) best = t;
  });
  return { url: location.href, title, company, description: best, location: pick(['[class*="location"]']) };
}

/* Setting .value directly does not notify React, and Handshake is React.
 * This is the native setter plus the events frameworks actually listen for
 * -- the same approach the server-side filler uses, for the same reason. */
function setValue(el, value) {
  const tag = el.tagName.toLowerCase();
  el.focus();
  if (tag === 'select') {
    const want = String(value).trim().toLowerCase();
    const hit = [...el.options].find(
      (o) => o.textContent.trim().toLowerCase() === want
        || o.value.trim().toLowerCase() === want);
    if (!hit) return false;
    el.value = hit.value;
  } else if (el.type === 'checkbox' || el.type === 'radio') {
    el.checked = ['true', 'yes', '1', 'on'].includes(String(value).toLowerCase());
  } else {
    const proto = tag === 'textarea' ? HTMLTextAreaElement : HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(proto.prototype, 'value')?.set;
    if (setter) setter.call(el, String(value));
    else el.value = String(value);
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.blur();
  return true;
}

/* A content script cannot hand a page a local path -- that is the file
 * input sandbox, and it is there for good reason. The PDF arrives as bytes
 * over loopback and is rebuilt into a File here, which is the one route
 * the platform does allow. */
function attach(el, { b64, filename, content_type }) {
  const raw = atob(b64);
  const buf = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) buf[i] = raw.charCodeAt(i);
  const file = new File([buf], filename, { type: content_type || 'application/pdf' });
  const dt = new DataTransfer();
  dt.items.add(file);
  el.files = dt.files;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}

function apply(prepared) {
  const result = { filled: [], failed: [], attached: null, skipped: [] };

  for (const a of prepared.answers || []) {
    const el = document.querySelector(a.selector);
    if (!el) { result.failed.push({ ...a, why: 'field is gone' }); continue; }
    // A guess is shown, never typed. A wrong answer to a work-authorisation
    // question on a form an employer reads is not a thing to be brave about.
    if (!a.certain) { result.skipped.push(a); continue; }
    if (setValue(el, a.value)) result.filled.push(a);
    else result.failed.push({ ...a, why: 'could not set the value' });
  }

  if (prepared.resume && prepared.resume.b64) {
    const input = [...document.querySelectorAll('input[type=file]')].find(visible)
      || document.querySelector('input[type=file]');
    if (input) {
      try {
        attach(input, prepared.resume);
        result.attached = prepared.resume.filename;
      } catch (e) {
        result.failed.push({ label: 'resume', why: String(e).slice(0, 120) });
      }
    } else {
      result.skipped.push({ label: 'resume',
        why: 'no file input on this page — Handshake may want a saved document' });
    }
  }
  return result;
}

/* The only entry point. The popup asks; nothing here runs on its own,
 * because a script that fills forms the moment a page loads is a script
 * nobody should install. */
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  try {
    if (msg?.type === 'handshake:scan') {
      reply({ ok: true, job: posting(), fields: harvest() });
    } else if (msg?.type === 'handshake:fill') {
      reply({ ok: true, result: apply(msg.prepared || {}) });
    } else {
      reply({ ok: false, error: 'unknown request' });
    }
  } catch (e) {
    reply({ ok: false, error: String(e).slice(0, 200) });
  }
  return true;
});
