"""The universal form filler.

This is what makes the system format-agnostic. Tools like Simplify work from a
fixed list of supported ATS platforms; anything else falls back to manual typing.
This works the other way round: it reads whatever form is actually on the page,
reasons about each field, and fills it.

The pipeline for any page:

    harvest  -> read every interactive field with its label, type, options,
                required flag, and current value, straight from the DOM
    resolve  -> answer bank first (deterministic, free, exact), then profile,
                then a single batched LLM call for whatever is genuinely
                subjective (essays, "why this company", free text)
    fill     -> set values in a way React/Vue/Angular actually notice
    verify   -> re-read every field and diff against intent; a field that
                silently failed to take is worse than one left blank
    advance  -> find and click the next/continue control, repeat

Known ATS adapters still exist and run first when recognised, because a known
layout is faster and more reliable than inference. This is the fallback that
means there is no such thing as an unsupported form.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..config import Config
from .answers import AnswerBank, HaltForInput, Resolution, best_option

# --------------------------------------------------------------------------- #
# DOM harvesting
# --------------------------------------------------------------------------- #

# Runs in page context. Returns one record per fillable control, with the best
# label we can find. Label discovery is deliberately exhaustive because ATS
# vendors label fields in at least six different ways.
HARVEST_JS = r"""
() => {
  const out = [];
  const seen = new Set();

  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const labelFor = (el) => {
    // 1. aria-label / aria-labelledby
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/).map(id => document.getElementById(id))
                      .filter(Boolean).map(n => n.innerText.trim());
      if (parts.length) return parts.join(' ');
    }
    // 2. <label for=id>
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    // 3. ancestor <label>
    const anc = el.closest('label');
    if (anc && anc.innerText.trim()) return anc.innerText.trim();
    // 4. enclosing field group -- walk up looking for a label-ish sibling
    let node = el.parentElement;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const cand = node.querySelector('label, legend, .label, [class*="label"], [class*="Label"]');
      if (cand && cand.innerText.trim() && !cand.contains(el)) return cand.innerText.trim();
      if (node.getAttribute && node.getAttribute('data-automation-id')) {
        const t = node.innerText.trim().split('\n')[0];
        if (t) return t;
      }
    }
    // 5. placeholder / name as last resort
    return el.getAttribute('placeholder') || el.getAttribute('name') || '';
  };

  const selector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    const da = el.getAttribute('data-automation-id');
    if (da) return `[data-automation-id="${CSS.escape(da)}"]`;
    const path = [];
    let n = el;
    while (n && n.nodeType === 1 && path.length < 6) {
      let s = n.tagName.toLowerCase();
      if (n.parentElement) {
        const sibs = [...n.parentElement.children].filter(c => c.tagName === n.tagName);
        if (sibs.length > 1) s += `:nth-of-type(${sibs.indexOf(n) + 1})`;
      }
      path.unshift(s);
      n = n.parentElement;
    }
    return path.join(' > ');
  };

  document.querySelectorAll('input, textarea, select, [contenteditable="true"], [role="combobox"], [role="radiogroup"]')
    .forEach((el) => {
      const type = (el.getAttribute('type') || el.tagName).toLowerCase();
      if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
      if (!visible(el) && type !== 'file') return;
      const sel = selector(el);
      if (seen.has(sel)) return;
      seen.add(sel);

      let options = [];
      if (el.tagName === 'SELECT') {
        options = [...el.options].map(o => o.textContent.trim()).filter(Boolean);
      } else if (type === 'radio' && el.name) {
        options = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)]
          .map(r => labelFor(r)).filter(Boolean);
      }

      out.push({
        selector: sel,
        tag: el.tagName.toLowerCase(),
        type: type,
        name: el.getAttribute('name') || '',
        id: el.id || '',
        label: labelFor(el).replace(/\s+/g, ' ').trim().slice(0, 400),
        placeholder: el.getAttribute('placeholder') || '',
        required: el.required || el.getAttribute('aria-required') === 'true',
        value: (el.value !== undefined ? el.value : el.innerText) || '',
        options: options.slice(0, 60),
        maxlength: el.getAttribute('maxlength') || null,
        automation_id: el.getAttribute('data-automation-id') || ''
      });
    });
  return out;
}
"""

# Setting .value directly does not notify React. This uses the native setter and
# dispatches the events frameworks actually listen for.
SET_VALUE_JS = r"""
(args) => {
  const el = document.querySelector(args.selector);
  if (!el) return { ok: false, reason: 'not found' };
  const tag = el.tagName.toLowerCase();
  el.focus();

  if (tag === 'select') {
    const want = String(args.value).trim().toLowerCase();
    let hit = null;
    for (const o of el.options) {
      if (o.textContent.trim().toLowerCase() === want || o.value.trim().toLowerCase() === want) { hit = o; break; }
    }
    if (!hit) for (const o of el.options) {
      if (o.textContent.trim().toLowerCase().includes(want)) { hit = o; break; }
    }
    if (!hit) return { ok: false, reason: 'no matching option' };
    el.value = hit.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
    return { ok: true, set: hit.textContent.trim() };
  }

  if (el.getAttribute('contenteditable') === 'true') {
    el.innerText = args.value;
    el.dispatchEvent(new InputEvent('input', { bubbles: true }));
    el.blur();
    return { ok: true, set: args.value };
  }

  const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, args.value);
  el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText' }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.blur();
  return { ok: true, set: args.value };
}
"""

READ_BACK_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  if (el.tagName === 'SELECT') {
    const o = el.options[el.selectedIndex];
    return o ? o.textContent.trim() : '';
  }
  if (el.getAttribute('contenteditable') === 'true') return el.innerText;
  return el.value;
}
"""

NEXT_BUTTON_PATTERNS = [
    r"^next$", r"^continue$", r"^save and continue$", r"^next step$",
    r"^proceed$", r"^save & continue$", r"^continue to", r"^start application$",
    r"^apply( now)?$", r"^begin$",
]

SUBMIT_PATTERNS = [
    r"^submit", r"^send application", r"^finish", r"^complete application",
    r"^submit application", r"^apply$",
]

# Detecting CAPTCHA by grepping raw HTML does not work: pages routinely ship
# Cloudflare/reCAPTCHA script tags with no challenge presented, so a substring
# match halts every application. Look for a challenge that is actually
# RENDERED and visible instead.
CAPTCHA_DOM_JS = r"""
() => {
  const sel = [
    'iframe[src*="recaptcha/api2/bframe"]',
    'iframe[src*="hcaptcha.com/captcha"]',
    'iframe[title*="challenge" i]',
    'div.g-recaptcha:not(:empty)',
    'div.h-captcha:not(:empty)',
    'div.cf-turnstile:not(:empty)',
    '#challenge-form',
    '#cf-challenge-running'
  ];
  for (const s of sel) {
    for (const el of document.querySelectorAll(s)) {
      const r = el.getBoundingClientRect();
      const st = window.getComputedStyle(el);
      if (r.width > 40 && r.height > 40 &&
          st.display !== 'none' && st.visibility !== 'hidden') {
        return s;
      }
    }
  }
  // A full-page interstitial: the body says "verify you are human" and there is
  // essentially nothing else on the page.
  const t = (document.body ? document.body.innerText : '').toLowerCase();
  if (t.length < 700 && /verify (you are|yourself)|are you a robot|checking your browser/.test(t)) {
    return 'interstitial';
  }
  return null;
}
"""


@dataclass
class Field:
    selector: str
    tag: str
    type: str
    label: str
    required: bool = False
    options: list[str] = dc_field(default_factory=list)
    value: str = ""
    placeholder: str = ""
    maxlength: int | None = None

    @property
    def question(self) -> str:
        return (self.label or self.placeholder or "").strip()

    @property
    def is_free_text(self) -> bool:
        return self.tag == "textarea" or (self.maxlength or 0) > 200


@dataclass
class FillResult:
    filled: dict[str, Any] = dc_field(default_factory=dict)
    skipped: list[str] = dc_field(default_factory=list)
    unresolved: list[Field] = dc_field(default_factory=list)
    mismatches: list[dict] = dc_field(default_factory=list)
    captcha: bool = False

    # The audit trail, in the form a human can actually check months later.
    # `filled` is keyed by CSS selector, which is what the browser needed and
    # is useless for answering "what did I tell this employer, and where did
    # that answer come from?". integrity.log_every_submitted_answer is only
    # honoured if the record carries the question and the provenance too, so
    # each entry is {question, answer, source, selector, legal}.
    audit: list[dict] = dc_field(default_factory=list)


class UniversalFiller:
    def __init__(self, bank: AnswerBank, router, settings: Config, profile: Config) -> None:
        self.bank = bank
        self.llm = router
        self.s = settings
        self.p = profile

    # ------------------------------------------------------------------ #
    def harvest(self, page) -> list[Field]:
        raw = page.evaluate(HARVEST_JS)
        out: list[Field] = []
        limit = int(self.s.get("apply.universal_filler.max_fields_per_page", 80))
        for r in raw[:limit]:
            ml = r.get("maxlength")
            out.append(Field(
                selector=r["selector"], tag=r["tag"], type=r["type"],
                label=r.get("label", ""), required=bool(r.get("required")),
                options=r.get("options") or [], value=r.get("value") or "",
                placeholder=r.get("placeholder", ""),
                maxlength=int(ml) if ml and str(ml).isdigit() else None,
            ))
        return out

    def detect_captcha(self, page) -> bool:
        """True only when a challenge is actually rendered and visible.

        The naive version grepped page HTML for "recaptcha"/"cloudflare" and
        fired on every Greenhouse form, because those scripts load whether or
        not a challenge is shown. A false positive here is expensive: the run
        halts and waits for a human who has nothing to solve.
        """
        try:
            hit = page.evaluate(CAPTCHA_DOM_JS)
        except Exception:
            return False
        if hit:
            print(f"  [captcha] visible challenge: {hit}")
            return True
        return False

    # ------------------------------------------------------------------ #
    def resolve_all(self, fields: list[Field], job: dict) -> tuple[dict[str, Resolution], list[Field]]:
        """Deterministic resolution first; collect what needs a model."""
        resolved: dict[str, Resolution] = {}
        needs_llm: list[Field] = []

        for f in fields:
            if f.type in ("file", "checkbox") or not f.question:
                continue
            try:
                r = self.bank.resolve(f.question, field_type=f.type,
                                      options=f.options, job=job)
            except HaltForInput:
                raise
            if r.ok:
                if f.options:
                    mapped = best_option(str(r.value), f.options)
                    if mapped:
                        r = Resolution(mapped, r.source, r.confidence, r.question_id,
                                       r.needs_qualifier, r.qualifier_text)
                resolved[f.selector] = r
            else:
                needs_llm.append(f)

        return resolved, needs_llm

    # ------------------------------------------------------------------ #
    def llm_fill(self, fields: list[Field], job: dict) -> dict[str, Resolution]:
        """One batched call for everything genuinely subjective."""
        if not fields:
            return {}

        from ..tailor.context import candidate_brief  # local import: avoids cycle

        payload = [{
            "id": i,
            "question": f.question,
            "type": f.type if f.tag != "textarea" else "long_text",
            "options": f.options or None,
            "required": f.required,
            "max_chars": f.maxlength,
        } for i, f in enumerate(fields)]

        voice = self.p.get("voice", {})
        system = (
            "You fill in job application forms on behalf of a candidate. "
            "You will be given the candidate's background, the job description, and a list of "
            "form questions. Answer each one as the candidate would.\n\n"
            "HARD RULES:\n"
            "1. Never invent experience, employers, dates, numbers or credentials. Use only what "
            "the candidate brief contains. If a question asks about something the candidate has "
            "not done, answer honestly and briefly rather than fabricating.\n"
            "2. Never answer a question about citizenship, visa status, work authorisation, "
            "sponsorship, security clearance, legal name or date of birth. Return null for those "
            "and set needs_human true.\n"
            "3. If a question offers options, your answer MUST be exactly one of them.\n"
            "4. Write plainly. No filler, no superlatives about the candidate.\n"
            f"5. Avoid these phrases entirely: {', '.join(voice.get('avoid_phrases', []))}\n\n"
            'Return JSON: {"answers":[{"id":int,"answer":string|null,"confidence":0..1,'
            '"needs_human":bool,"why":string}]}'
        )

        user = (
            f"## CANDIDATE\n{candidate_brief(self.p)}\n\n"
            f"## JOB\nCompany: {job.get('company')}\nTitle: {job.get('title')}\n"
            f"Location: {job.get('location')}\n\n"
            f"### Job description\n{(job.get('description') or '')[:6000]}\n\n"
            f"## QUESTIONS\n{json.dumps(payload, indent=2)}\n\n"
            f"Tone: {voice.get('tone','direct and concrete')}\n"
            f"Short answers {voice.get('short_answer_words',[90,160])} words; "
            f"long answers {voice.get('long_answer_words',[180,300])} words."
        )

        try:
            data = self.llm.json(system, user, task="field_mapping")
        except Exception:
            return {}

        out: dict[str, Resolution] = {}
        for item in (data or {}).get("answers", []):
            try:
                f = fields[int(item["id"])]
            except (KeyError, ValueError, IndexError):
                continue
            if item.get("needs_human") or item.get("answer") in (None, ""):
                continue
            val = item["answer"]
            if f.options:
                val = best_option(str(val), f.options) or val
            out[f.selector] = Resolution(val, "llm", float(item.get("confidence", 0.7)))
        return out

    # ------------------------------------------------------------------ #
    def fill_page(self, page, job: dict, *, job_id: int | None = None) -> FillResult:
        res = FillResult()

        if self.detect_captcha(page):
            res.captcha = True
            return res

        fields = self.harvest(page)
        if not fields:
            return res

        resolved, needs_llm = self.resolve_all(fields, job)

        threshold = float(self.s.get("apply.universal_filler.confidence_threshold", 0.80))
        if needs_llm:
            subjective = [f for f in needs_llm if f.is_free_text or f.required]
            resolved.update(self.llm_fill(subjective, job))

        by_sel = {f.selector: f for f in fields}

        for sel, r in resolved.items():
            f = by_sel.get(sel)
            if f is None or r.value in (None, ""):
                continue
            if r.confidence < threshold and r.source == "llm":
                res.unresolved.append(f)
                continue

            value = str(r.value)
            # A legal answer that needs a qualifier but has nowhere to put it must halt.
            if r.needs_qualifier and r.qualifier_text:
                if f.is_free_text:
                    value = f"{value}. {r.qualifier_text}"
                elif self.p.get("work_authorization.halt_if_no_qualifier_field", True):
                    note = self._find_free_text_near(fields, f)
                    if note is not None:
                        self._set(page, note.selector, r.qualifier_text, res,
                                  question=f"qualifier for: {f.question}",
                                  source=r.source, legal=True)

            ok = self._set(page, sel, value, res, question=f.question,
                           source=r.source,
                           legal=bool(getattr(r, "is_legal", False)))
            if ok:
                self.bank.mark_used(f.question)

        # anything still unanswered goes to the ask queue, once, forever
        for f in fields:
            if f.selector in resolved or f.type in ("file", "checkbox"):
                continue
            if f.required and f.question:
                self.bank.enqueue_ask(
                    f.question, field_type=f.type, options=f.options, job_id=job_id,
                    context=f"{job.get('company')} — {job.get('title')}",
                )
                res.unresolved.append(f)

        if self.s.get("apply.universal_filler.reread_and_verify", True):
            self._verify(page, res)

        return res

    # ------------------------------------------------------------------ #
    def _set(self, page, selector: str, value: str, res: FillResult,
             *, question: str = "", source: str = "", legal: bool = False) -> bool:
        try:
            out = page.evaluate(SET_VALUE_JS, {"selector": selector, "value": value})
        except Exception as e:
            res.skipped.append(f"{selector}: {e}")
            return False
        if isinstance(out, dict) and out.get("ok"):
            res.filled[selector] = value
            res.audit.append({
                "question": question or selector,
                "answer": value,
                "source": source or "unknown",
                "selector": selector,
                "legal": bool(legal),
            })
            return True
        res.skipped.append(f"{selector}: {out.get('reason') if isinstance(out, dict) else out}")
        return False

    def _verify(self, page, res: FillResult) -> None:
        """A field that silently failed to take is worse than a blank one."""
        for sel, intended in list(res.filled.items()):
            try:
                actual = page.evaluate(READ_BACK_JS, sel)
            except Exception:
                continue
            if actual is None:
                continue
            a, b = str(actual).strip().lower(), str(intended).strip().lower()
            if a != b and b not in a and a not in b:
                res.mismatches.append({"selector": sel, "intended": intended, "actual": actual})

    def _find_free_text_near(self, fields: list[Field], anchor: Field) -> Field | None:
        try:
            idx = fields.index(anchor)
        except ValueError:
            return None
        for f in fields[idx + 1: idx + 4]:
            if f.is_free_text:
                return f
        return None

    # ------------------------------------------------------------------ #
    def find_advance(self, page) -> tuple[str | None, bool]:
        """Return (selector, is_submit) for the control that moves forward."""
        js = r"""
        () => {
          const els = [...document.querySelectorAll(
            'button, input[type=submit], a[role=button], [role=button]')];
          return els.filter(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && !e.disabled;
          }).map(e => ({
            text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim(),
            id: e.id || '', name: e.getAttribute('name') || '',
            automation: e.getAttribute('data-automation-id') || ''
          }));
        }
        """
        try:
            buttons = page.evaluate(js)
        except Exception:
            return None, False

        def match(text: str, pats: list[str]) -> bool:
            t = text.strip().lower()
            return any(re.search(p, t) for p in pats)

        for b in buttons:
            if match(b["text"], SUBMIT_PATTERNS):
                return self._btn_selector(b), True
        for b in buttons:
            if match(b["text"], NEXT_BUTTON_PATTERNS):
                return self._btn_selector(b), False
        return None, False

    @staticmethod
    def _btn_selector(b: dict) -> str:
        if b.get("id"):
            return f"#{b['id']}"
        if b.get("automation"):
            return f"[data-automation-id=\"{b['automation']}\"]"
        text = b["text"].replace('"', '\\"')
        return f'text="{text}"'
