You are helping me build a **master resume**: one file holding every bullet I
have ever been able to truthfully write about my work, from which a separate
program selects a subset per job application and fits it onto one page.

This is not a resume. It is the source a resume gets *selected from*, so it
should be deliberately longer than one page — roughly two to three times the
content that would fit. The selection engine picks and shortens; it never
writes. That means anything not in this file can never appear on any resume I
send, and anything in it must be defensible in an interview.

## What I am giving you

1. My current resume (attached).
2. My personal site: **<YOUR_SITE_URL>** — read it if you can. If you cannot
   fetch it, say so and ask me to paste the text rather than guessing at what
   it says.

Use both. The site usually carries detail the resume had no room for —
technical skills, project write-ups, longer descriptions — and that detail is
exactly what a master resume is for.

## About me, for context

<!-- Replace this paragraph with your own situation. The more specific, the
     better the tagging: degree and year decide which roles read as plausible,
     and the target areas decide which tags matter. -->

<YEAR> at <UNIVERSITY>, Class of <GRAD_YEAR>, studying <MAJORS>.
<CITIZENSHIP_OR_WORK_AUTHORISATION, if it constrains where you can work>.
Targeting **<SEASON AND YEAR>** internships in <TARGET_AREAS>, and
<open / not open> to non-<COUNTRY> markets.

---

# Material that is not on my resume yet

<!-- Paste anything newer than the attached resume here: a role you just
     started, a project you just shipped, research in progress, a competition
     you placed in. This section is primary source material and should win over
     the PDF wherever they disagree.

     Three kinds of entry mislead very easily. If any of yours is one of these,
     keep the matching guardrail — each has been load-bearing in practice. -->

## <Thing one>

<The facts. Be concrete and be boring: what the system does, what the role
involves, what the dataset is. Save the judgement for the bullets.>

---

### Guardrails worth copying

**A team project.** State who did what, or say that you do not know. If a deck
or repo does not record ownership, tell the model:

> This was a team of N and the sources do not record who built what. Do not
> assign any of it to me. Write the entry from project-level facts, then ask me
> component by component which parts were mine. Until I answer, phrase bullets
> at team level and mark each `TODO: confirm my ownership`.

A win or a shipped product is a strong credential right up until an interviewer
asks which part you wrote and the answer does not match the resume.

**Research in progress.** A proposal's hypotheses and expected results are
predictions. Tell the model:

> Do not write a bullet that states an expected result as one I obtained. Build
> the entry from what is true now: the question, the dataset and its real
> dimensions, the methods being implemented, the design decisions. Ask me what
> has actually run.

The question, the data and the method are already a strong entry with no
findings in it at all.

**A role you have given no numbers for.** Tell the model:

> I have given you no metrics for this. Do not supply any. Write the bullets
> without them and ask me for the specific figures you want; I can look up real
> ones.

An invented "resolved 200+ tickets" is the exact failure this file exists to
prevent, and it is the easiest one to produce by accident.

---

# Output format — exact, and non-negotiable

The file is parsed by a strict importer. Deviations are silently dropped or
misread, so follow this grammar literally.

```
## Experience

### Software Engineer Intern — Acme Corp    [pin]
id: exp_acme
org: Platform Team
location: Boston, MA
dates: Jun 2026 -- Aug 2026
tags: python, backend, api, performance, testing, postgres
lead_for: default

- Full-length bullet, written naturally, with any numbers inline    [pin]
  ~ A medium-length version of the same bullet, one clause shorter.
  ~~ A short version, under about twelve words.
- The next bullet for this same role
  ~ Its medium version.
  ~~ Its short version.
```

### The rules that matter

**Sections.** Use only these `##` headings, in this order: `Skills`,
`Experience`, `Projects`, `Leadership`, `Education`. Put honours and awards as
an entry *inside* `## Education`, not as their own section.

**`## Skills` is formatted differently** — no `###` entries, no bullets. It is
`Group: comma, separated, items`, one group per line:

```
## Skills

Languages: Python, C++, TypeScript, SQL
ML: PyTorch, NumPy, scikit-learn
Systems: Linux, Docker, Git
```

Every line in this section needs a colon, or it is ignored.

**Variant lines must be indented.** `  ~ text` and `  ~~ text` require leading
whitespace. Unindented, they are not recognised as variants. This is the single
easiest thing to get wrong.

**`[pin]` goes at the end of the line**, after the heading text or the bullet
text, nowhere else.

**Metadata keys are lowercase with underscores**, one per line, placed between
the `###` heading and the first bullet. Valid keys: `id`, `org`, `location`,
`dates`, `tags`, `lead_for`, `stack`, `url`.

> **Trap:** *any* line matching `lowercase_word:` becomes metadata. A bullet
> that loses its leading `- ` and happens to contain a colon will vanish into a
> metadata field. Every bullet starts with `- `.

**Do not put comments inside an entry.** A `<!-- -->` block after a `###`
heading gets parsed as content.

---

# Content rules

### Bullets — quantity is the point

Write **4 to 6 bullets for every substantial role or project**, and 2 to 3 for
minor ones. More than would fit on a page, on purpose: the engine needs choices.
Make them *different in kind*, not rephrasings of each other — so that different
jobs select different ones. For a single role, aim to cover:

- the hardest technical problem and how it was solved
- a measurable outcome
- scale, scope, or constraint (data size, users, deadline, hardware)
- collaboration, ownership, or communication
- the tooling or engineering practice (testing, CI, profiling, review)

### Variants — write them for every bullet

Each bullet gets a `~` (medium) and a `~~` (short) version. When a page is
crowded the engine *shortens* rather than dropping, so a bullet with variants
survives where one without it gets cut. The short version must still be a
complete, true claim — not a truncation.

### `tags:` — this is what actually matches jobs

Tags are scored against the job description, so they decide what gets picked.
Give each entry **6 to 12** lowercase tags. Be specific and literal: name the
actual languages, libraries, and concepts (`pytorch`, `cuda`, `distributed`,
`threat-modeling`, `numerical-methods`), not vague categories (`coding`,
`teamwork`). Include terms a recruiter would search for even if my bullet
phrases them differently.

### `lead_for:` — use sparingly

Which job families this entry should *lead* the resume for. Valid values only:
`ai_ml`, `quant`, `security`, `graphics`, `research`, `default`. Set it only
where the entry is genuinely my strongest evidence for that family. Most
entries should have no `lead_for` at all.

### `[pin]` — at most five in the whole file

A pin means "appears on every single resume, regardless of the job". Pin my
single strongest role and at most two or three individual bullets. Over-pinning
defeats tailoring entirely — if the pinned set alone overflows a page, the
engine reports the overflow instead of choosing for me, and then I have to come
back and fix it.

### `id:` — stable, and set on every entry

Format: `exp_`, `project_`, `leadership_`, or `edu_` plus a short slug
(`exp_acme`, `project_raytracer`). These are the permanent identity of an entry
across re-imports. Never reuse one for a different thing.

---

# Truthfulness — the hard constraint

Numbers written here get extracted into a ledger and reused on real
applications, some of which carry statements that reappear on immigration
paperwork. So:

- **Never invent a number.** Not a plausible one, not a conservative one, not a
  placeholder. If a bullet would be stronger with a metric I have not given
  you, write the bullet without it and list the gap in your report.
- **Never upgrade a verb.** "Contributed to" does not become "led". "Helped
  build" does not become "architected". Use the ownership level my sources
  actually support.
- **Keep numerals in the form I wrote them.** Write `35%`, `216x`, `60 FPS`,
  `25+`, `3 days` — using the `%`, `x`, `+` symbols and plain unit words. The
  extractor recognises those; spelled-out forms like "thirty-five percent" are
  missed, and a missed number is one the renderer cannot verify.
- **Do not merge two facts into one bullet** to make it sound bigger. Each
  bullet should be separately defensible.

### Where my two sources disagree

My resume and my website contradict each other in places — dates, metrics, and
job titles among them. When you find a conflict:

1. Use the **resume** value in the file, since that is what employers have seen.
2. List every conflict in your report, with both values and where each came
   from, so I can decide which is right and fix the other source.

Do not quietly pick one. A contradiction between my resume and a site a
recruiter can find is worse than either value alone.

### If something is ambiguous

Ask. A question costs me ten seconds; a fabricated detail costs me an interview
I will not know I lost. Put a `TODO:` marker in the bullet and raise it in your
report rather than filling the gap yourself.

---

# What to give me back

**First**, the complete file in a single fenced code block, ready to save as
`config/bank/master_resume.md` — nothing before or after it inside the block.

**Then**, outside the block, a short report:

1. **Conflicts** between my resume and my website, with both values.
2. **Missing metrics** — bullets that would be materially stronger with a
   number I have not supplied. Ask me for the specific figure.
3. **Metrics I should sanity-check** — any claim that would be awkward to
   defend under interview questioning, and why.
4. **Thin coverage** — which of my four target areas (software engineering,
   AI/ML, cybersecurity, quantitative) has the least evidence behind it, since
   that tells me what to build next.
5. **Anything you left out** of the file, and why.

Keep the report short. I will act on it, not read it twice.

---

# Before you finish, check your own output

- Does every `###` entry have an `id:`, a `tags:` line, and at least one `- ` bullet?
- Does every bullet start with `- ` and have both a `~` and a `~~` variant, each indented?
- Are there 4–6 bullets on each substantial entry, covering genuinely different ground?
- Are there **five or fewer** `[pin]` markers in the entire file?
- Does `## Skills` contain only `Group: a, b, c` lines, with no `###` and no bullets?
- Is every `lead_for:` value one of `ai_ml`, `quant`, `security`, `graphics`, `research`, `default`?
- Does every number in the file trace to something in my resume or on my site —
  with no exceptions, and nothing rounded "for readability"?
- Is the whole file inside one code block?
