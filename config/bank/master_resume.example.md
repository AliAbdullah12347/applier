<!--
==============================================================================
MASTER RESUME — TEMPLATE. Copy to master_resume.md and fill with your own work.

Put EVERYTHING here. Every role, every project, every bullet you have ever
written. There is no length limit, and nothing here is ever deleted by the
system. The tailoring engine picks a subset per job and fits one page.

    applier bank import            rebuild the content bank from this file
    applier bank lint              check it is renderable and traceable
    applier bank preview <url>     see what a specific job would select

HOW TO ADD SOMETHING
  Copy a block. The only required parts are the ### heading and one "- " bullet.

METADATA (all optional)
  org / location / dates      role heading details
  stack / url                 for projects
  tags:      what this is ABOUT. This is what matches against job descriptions,
             so be generous and specific: python, pytorch, distributed, security
  lead_for:  job families where this should LEAD the resume.
             ai_ml | quant | security | graphics | game | research | default
  id:        stable identifier. Set it once so re-imports keep claim links.

NON-NEGOTIABLES
  [pin] after a heading  -> this role ALWAYS appears, on every resume
  [pin] after a bullet   -> this bullet ALWAYS appears
  Pin sparingly. If the pinned set alone overflows a page, the engine reports
  which pins did not fit rather than silently dropping them — only you can
  decide what gives.

SHORTER VARIANTS (optional but worth doing for your best bullets)
  Indent "~" under a bullet for a medium version, "~~" for a short one. When
  space is tight the engine shortens instead of dropping, so a bullet with
  variants survives a crowded page that would otherwise cut it.

NUMBERS
  Write them inline and naturally. The importer extracts every numeral into
  claims.yaml and replaces it with a slot, so the renderer physically cannot
  emit an unregistered figure. New numbers arrive as needs_check for you to
  confirm; anything you mark RETIRED stays retired, and re-importing never
  brings a withdrawn metric back.
==============================================================================
-->

## Skills

Languages: Python, TypeScript, SQL
Frameworks: React, FastAPI
Data: PostgreSQL, SQLite
Tools: Git, Linux, Docker


## Experience

### Software Engineer Intern — Example Corp    [pin]
id: exp_example
org: Platform Team
location: City, ST
dates: Jun 2025 -- Aug 2025
tags: python, backend, api, performance, testing, distributed
lead_for: default

- Cut nightly batch runtime from 3 hours to 11 minutes by replacing per-row database lookups with a precomputed in-memory index, removing the job from the critical path of the morning report    [pin]
  ~ Cut nightly batch runtime from 3 hours to 11 minutes by replacing per-row lookups with a precomputed index.
  ~~ Cut a nightly batch job from 3 hours to 11 minutes.
- Caught 25+ edge-case failures through systematic regression testing, and added the reproductions to CI so the same class of bug could not recur
  ~~ Found 25+ edge-case failures through regression testing.


## Projects

### Example Project
id: project_example
stack: TypeScript, React, PostgreSQL
url: https://github.com/you/example
dates: 2025
tags: typescript, react, fullstack, api, auth

- Built and deployed a full-stack application with authentication, durable storage and a typed API layer, used by 120 people in its first month
  ~~ Full-stack app with authentication and durable storage.


## Leadership

### Lead — Example Organisation
id: leadership_example
org: Your University
dates: 2025 -- Present
tags: leadership, organizing, communication

- Grew membership from 60 to 140 by launching recurring programming and managing a $4,000 annual budget


## Education

### Honors & Awards
id: honors
tags: academic, competition, selective

- Named scholar, selected as 1 of 16 in the incoming class    [pin]
