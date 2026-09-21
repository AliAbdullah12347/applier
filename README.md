# applier

An end-to-end autonomous job application system. It discovers roles that match a
declared profile, tailors a resume and cover letter to each posting, fills the
application form — **whatever form it happens to be** — and tracks what happens next.

> **Status:** active development. The architecture and safety model are settled;
> individual subsystems are being filled in. See [Roadmap](#roadmap).

---

## Why this exists

Most job-application tools are autofill extensions with a hard-coded list of
supported platforms. Hit an ATS they don't support — or a company's bespoke
careers page — and you're typing by hand again.

`applier` inverts that. It reads whatever form is actually rendered, reasons
about each field, and fills it. Known platforms get a fast path; everything else
falls through to a universal filler. There is no "unsupported" case.

It is also built around a constraint most of these tools ignore: **a job
application is a set of factual claims about a person.** Some of those claims get
repeated later on government forms. So the system is designed so that it
*structurally cannot* emit a claim that isn't traceable to something the user
declared as true.

---

## How it works

```
discover ──► gate ──► score ──► tailor ──► fill ──► verify ──► submit ──► track
```

| Stage | What happens |
|---|---|
| **discover** | Polls unauthenticated ATS JSON APIs (Greenhouse, Lever, Ashby, Workable, Recruitee) plus aggregate feeds. Mines apply-URLs for board tokens, which is free company discovery at scale. |
| **gate** | Hard eligibility first — work authorisation, graduation window, clearance requirements, per-employer caps. A gate failure zeroes the score rather than weighting it. Running this *before* tailoring is the single biggest time saver. |
| **score** | BM25 over the content bank plus title, location and sponsorship signals. |
| **tailor** | Selects and orders pre-authored phrasings from a content bank. Never generates resume prose. Renders through LaTeX. |
| **fill** | Live DOM introspection, deterministic resolution first, one batched model call for genuinely subjective questions. |
| **verify** | Re-extracts the generated PDF and checks it the way a parser sees it. Failures delete the artifact. |
| **track** | SQLite record of every application, every answer submitted, and every outcome. |

Verified working against live data: a discovery run pulled 400 real postings and
mined 151 ATS board tokens from their apply URLs (71 Greenhouse, 25 Lever, 48
Ashby, 7 Workable) at zero cost, then gated and ranked them. A tailored resume
renders to a verified single page in about six seconds.

---

## Design decisions worth knowing

**Selection, not generation.** A model asked to "tailor this bullet" will quietly
upgrade *contributed to* into *led*, or invent a percentage that reads well. So
the tailoring engine only picks among phrasings the user wrote in advance.
Numbers live in a separate claims ledger and appear in phrasings as `{{C.path}}`
slots — a phrasing containing a bare numeral fails lint. Retiring a claim makes
every phrasing that used it unrenderable, so a withdrawn metric cannot reappear.

**Legal fields never touch a model.** Citizenship, work authorisation, visa
status, legal name, date of birth and graduation date are copied verbatim from
the profile. No fuzzy matching, no inference. If classification is ambiguous the
run halts and asks. These are stored once and reused forever, so this costs
nothing in autonomy.

**Gate before you spend.** Aggregate job feeds carry only a title and a URL. Every
eligibility check that matters — citizenship requirements, security clearance,
ITAR, explicit "we do not sponsor" — lives in the description body, so the system
fetches the real description before gating, and refuses to auto-apply to any
posting whose gates could not actually run.

**Verification is adversarial.** After every compile the PDF is re-extracted and
checked for hyphen-split keywords (pdflatex breaks words in the *text layer*,
and parsers don't rejoin them), replacement characters, missing contact details,
and untraceable numerals. It also refuses to ship a PDF containing invisible
text — near-white, sub-3pt, or positioned off-page — because hidden keyword
blocks are detected in production and detection surfaces the attempt to a human.

**Human-controlled boundaries.** The system does not solve CAPTCHAs (it detects,
pauses, notifies), does not automate LinkedIn/Indeed/Glassdoor/Handshake, and
drafts outreach messages without sending them.

**It runs on your machine, headed.** Major ATS platforms flag datacenter IPs and
timezone mismatches as fraud signals, so cloud VMs and headless runs get
applications quietly binned.

---

## Quick start

```bash
git clone https://github.com/<you>/applier.git
cd applier
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -r requirements.txt
python -m playwright install chromium

cp config/profile.example.yaml config/profile.yaml
applier setup      # one-time wizard: asks for everything missing, once
applier doctor     # verifies keys, deps, disk, pdflatex
```

Then open the app:

```bash
applier gui
```

or drive it from the terminal:

```bash
applier apply https://job.example.com/postings/123   # one link, start to finish
applier run                                          # autonomous: find and apply
```

Requires Python 3.11+, a TeX distribution providing `pdflatex`, and an API key
for any one supported LLM provider.

---

## The app

`applier gui` serves a local web UI and prints a link to open. Everything the
CLI can do is in it, and neither side is the poor relation — a test fails if a
capability lands on one and not the other.

| Screen | What it is for |
|---|---|
| **Dashboard** | The funnel, and whatever is currently blocking progress, listed before the statistics. One system check that makes a real model call. |
| **Jobs** | The queue, sorted by score. Each row explains itself: the gate verdict and its reason, or a warning that the posting was too thin to gate at all. |
| **Apply** | Paste a link, choose how much autonomy to grant this run, watch the log live. Below it, the unattended runner with a stop button. |
| **Applications** | What an employer actually received: the resume hash, every field submitted with its provenance, a screenshot of each step. |
| **Questions** | Anything it could not resolve. Answer once; it is never asked again. Plus the bank of everything already learned. |
| **Resume** | The master markdown file, and a preview of what a given posting would select from it — including which of its keywords nothing in your bank covers. |
| **Outreach** | Contacts and drafted messages. Nothing is ever sent; there is no send path in the codebase. |
| **Settings** | Autonomy, keys, limits, profile, and a privacy audit you can run from the page. |

### Autonomy

One dial, four positions, shown on the dashboard and in Settings:

| Level | What it does |
|---|---|
| **Fully involved** | Finds, scores, tailors, writes the letter. Opens nothing, sends nothing. |
| **Review each one** | Fills every field and stops with the browser on screen. You press submit. |
| **Assisted** | Submits strong matches; anything weaker waits for your approval. |
| **Fully autonomous** | Submits everything that clears the gates. |

What the dial does *not* change, at any setting: eligibility gates still run, a
legal or work-authorisation question that cannot be resolved exactly still halts
that application rather than being guessed at, a visible CAPTCHA still pauses,
and the per-employer caps hold.

### Why it is safe to run a server on your own machine

A page on any website you have open can send requests to `127.0.0.1`, and with a
DNS rebind it can read the replies — which here would mean a date of birth, a
home address and an immigration status. So the GUI:

- binds loopback only, and refuses to start on any other address;
- requires a token minted per launch, delivered in the URL **fragment** so it
  never reaches a server log, and held in `sessionStorage`, never on disk;
- allow-lists the `Host` header, which is what actually stops a rebind;
- sends no CORS headers at all, so every cross-origin preflight fails closed;
- ships a CSP with no remote origins and no `unsafe-inline` — the UI loads no
  CDN, no web font, and no third-party script;
- never returns a stored secret from any endpoint.

`tests/test_web_security.py` covers each of these and names the attack it stops.

---

## Configuration

Everything lives in two files, and the defaults are meant to be usable as-is.

- **`config/settings.yaml`** — LLM routing and fallbacks, discovery sources,
  eligibility gates, per-employer caps, tailoring guardrails, browser policy,
  integrity rules, scheduling.
- **`config/profile.yaml`** — who you are. Gitignored; never leaves your machine.

Any value can be overridden by environment variable:

```bash
APPLIER__LLM__PRIMARY__MODEL=gpt-5
APPLIER__APPLY__MAX_PER_DAY=10
```

Swapping LLM provider is three lines:

```yaml
llm:
  primary:
    provider: "anthropic"      # gemini | openai | anthropic | openrouter | groq
    model: "claude-sonnet-5"   # | mistral | together | cerebras | deepseek | ollama
    api_key_env: "ANTHROPIC_API_KEY"
```

API keys are read from the environment or the OS keychain — never from the YAML,
so `settings.yaml` stays safe to commit.

---

## Privacy

The repository is designed to be safe to publish. All personal data —
`config/profile.yaml`, the content bank, generated documents, the database, the
browser profile — is gitignored and stays local. Site passwords go to the OS
keychain (Windows Credential Manager / macOS Keychain / Secret Service); the
database stores only a keychain reference, so a stolen `applier.db` yields
usernames and nothing else.

When PII redaction is enabled, identity is stripped before any model call and
re-attached locally at render time, so only the job description and the content
bank leave the machine.

---

## Roadmap

- [x] Configuration, datastore, LLM routing
- [x] Answer bank with legal-field locking
- [x] Universal form filler
- [x] Eligibility gates and scoring
- [x] Account creation with keychain storage and IMAP verification
- [x] Content bank, LaTeX rendering, verification gate
- [x] Cover letter and free-text answer generation
- [x] Per-ATS fast-path adapters (11 platforms + generic fallback)
- [x] Outreach: contact discovery and message drafting
- [x] Description enrichment so eligibility gates run on real text
- [ ] Gmail outcome tracking
- [ ] Weekly intelligence digest
- [ ] Market-trend analysis and project recommendations

---

## License

MIT
