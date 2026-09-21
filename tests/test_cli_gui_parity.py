"""The CLI and the GUI must stay in step.

Ali asked for one system with two front doors, not two products that slowly
stop agreeing with each other. The realistic failure is not dramatic: someone
adds a command, forgets the panel, and six weeks later the GUI is quietly the
lesser of the two and nobody has noticed.

So parity is a test. Adding a capability to one side without the other fails
here, and the failure message says which side is missing.

The mapping is explicit rather than inferred. A clever auto-matcher would
either miss real gaps or invent false ones, and a list you have to edit is the
point: editing it is the moment you decide what the other front door should do.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "applier" / "cli.py"
SERVER = ROOT / "applier" / "web" / "server.py"
STATIC = ROOT / "applier" / "web" / "static"

# CLI command  ->  the GUI route(s) that do the same job.
# An empty tuple means "deliberately terminal-only", with a reason.
PARITY: dict[str, tuple[str, ...]] = {
    "gui":      (),                       # the GUI cannot launch itself
    "setup":    ("PUT /api/profile", "PUT /api/secrets"),
    "doctor":   ("POST /api/doctor",),
    "apply":    ("POST /api/apply",),
    "discover": ("POST /api/discover",),
    "rank":     ("POST /api/rank",),
    "run":      ("POST /api/run",),
    "asks":     ("GET /api/asks", "POST /api/asks/{ask_id}"),
    "status":   ("GET /api/state",),
    "outreach-harvest": ("POST /api/outreach/harvest",),
    "outreach-draft":   ("POST /api/outreach/draft",),
    "outreach-list":    ("GET /api/outreach",),
    "bank-import":  ("POST /api/bank/import",),
    "bank-lint":    ("GET /api/bank",),
    "bank-preview": ("POST /api/bank/preview",),
    "config-set-key": ("PUT /api/secrets",),
    "config-show":    ("GET /api/settings",),
}

# GUI-only routes, and why the CLI does not need them.
GUI_ONLY = {
    "GET /api/paths":                       "a display aid",
    "GET /api/events":                      "the CLI prints events as they happen",
    "GET /api/jobs":                        "`rank` prints the same table",
    "GET /api/jobs/{job_id}":               "no CLI equivalent yet",
    "POST /api/jobs/{job_id}/status":       "no CLI equivalent yet",
    "POST /api/jobs/{job_id}/apply":        "`apply <url>` covers it",
    "GET /api/tasks":                       "the CLI is the task",
    "GET /api/tasks/{task_id}":             "the CLI is the task",
    "POST /api/tasks/{task_id}/stop":       "Ctrl-C",
    "GET /api/applications":                "`status` summarises them",
    "GET /api/applications/{app_id}":       "the artifact directory is on disk",
    "POST /api/applications/{app_id}/outcome": "no CLI equivalent yet",
    "DELETE /api/asks/{ask_id}":            "no CLI equivalent yet",
    "GET /api/answers":                     "no CLI equivalent yet",
    "PATCH /api/answers/{answer_id}":       "no CLI equivalent yet",
    "DELETE /api/answers/{answer_id}":      "no CLI equivalent yet",
    "GET /api/bank":                        "covered by bank lint",
    "PUT /api/bank":                        "you edit the file directly",
    "POST /api/outreach/{contact_id}/stage": "no CLI equivalent yet",
    "PUT /api/settings":                    "you edit settings.yaml directly",
    "POST /api/autonomy":                   "settings.yaml, or --review/--dry-run",
    "GET /api/profile":                     "covered by setup",
    "GET /api/secrets":                     "`doctor` reports whether a key is set",
    "POST /api/audit":                      "scripts/audit_privacy.py",
}


def _cli_commands() -> set[str]:
    """Every @app.command / @<sub>_app.command in cli.py, by its public name."""
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    prefixes = {"app": "", "config_app": "config-", "reach_app": "outreach-",
                "bank_app": "bank-"}
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            attr = call.func if call else dec
            if not isinstance(attr, ast.Attribute) or attr.attr != "command":
                continue
            owner = attr.value.id if isinstance(attr.value, ast.Name) else ""
            if owner not in prefixes:
                continue
            explicit = None
            if call and call.args and isinstance(call.args[0], ast.Constant):
                explicit = call.args[0].value
            found.add(prefixes[owner] + (explicit or node.name).replace("_", "-")
                      if owner != "app" else (explicit or node.name).replace("_", "-"))
    return found


def _gui_routes() -> set[str]:
    """Every route(...) registration in server.py, as 'METHOD /path'."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # route("GET", "/api/x")(handler) -> the outer call wraps the inner one
        inner = fn if isinstance(fn, ast.Call) else None
        if inner and isinstance(inner.func, ast.Name) and inner.func.id == "route":
            args = inner.args
            if len(args) >= 2 and all(isinstance(a, ast.Constant) for a in args[:2]):
                out.add(f"{args[0].value} {args[1].value}")
    return out


# --------------------------------------------------------------------------- #
def test_every_cli_command_is_listed():
    """A new command must be given a GUI route, or an explicit exemption."""
    missing = _cli_commands() - set(PARITY)
    assert not missing, (
        f"new CLI command(s) with no entry in PARITY: {sorted(missing)}.\n"
        f"Add the matching GUI route, or map it to () with a reason.")


def test_no_stale_parity_entries():
    """A removed command should not leave a phantom obligation behind."""
    stale = set(PARITY) - _cli_commands()
    assert not stale, f"PARITY lists command(s) that no longer exist: {sorted(stale)}"


def test_every_promised_gui_route_exists():
    """The heart of it: a CLI capability the GUI claims to cover must be real."""
    routes = _gui_routes()
    broken = {}
    for cmd, promised in PARITY.items():
        gaps = [r for r in promised if r not in routes]
        if gaps:
            broken[cmd] = gaps
    assert not broken, (
        "the GUI is missing route(s) that PARITY says cover a CLI command:\n"
        + "\n".join(f"  {c}: {g}" for c, g in broken.items()))


def test_every_gui_route_is_accounted_for():
    """And the reverse: a route is either CLI parity or a declared GUI-only."""
    promised = {r for rs in PARITY.values() for r in rs}
    unexplained = _gui_routes() - promised - set(GUI_ONLY)
    assert not unexplained, (
        f"GUI route(s) with no CLI counterpart and no GUI_ONLY note: "
        f"{sorted(unexplained)}.\nAdd the CLI command, or record why it is "
        f"GUI-only.")


def test_no_stale_gui_only_entries():
    stale = set(GUI_ONLY) - _gui_routes()
    assert not stale, f"GUI_ONLY lists route(s) that no longer exist: {sorted(stale)}"


# --------------------------------------------------------------------------- #
# the panels themselves
# --------------------------------------------------------------------------- #
PANELS = ["dashboard", "jobs", "apply", "applications", "asks",
          "resume", "outreach", "settings"]


def test_every_panel_in_the_nav_exists_on_disk():
    """The shell imports these lazily, so a typo is invisible until clicked."""
    shell = (STATIC / "app.js").read_text(encoding="utf-8")
    for p in PANELS:
        assert f"'{p}'" in shell, f"{p} is not registered in app.js"
        assert (STATIC / "panels" / f"{p}.js").is_file(), f"panels/{p}.js is missing"


def test_every_panel_exports_render():
    for p in PANELS:
        src = (STATIC / "panels" / f"{p}.js").read_text(encoding="utf-8")
        assert "export async function render(" in src or \
               "export function render(" in src, f"{p}.js does not export render()"


@pytest.mark.parametrize("panel", PANELS)
def test_no_panel_uses_a_markup_sink(panel):
    """The UI renders untrusted employer text. It must never build markup."""
    src = (STATIC / "panels" / f"{panel}.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                 "document.write", "eval(", "new Function"):
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("*", "//", "/*")):
                continue            # a comment naming the sink is fine
            assert sink not in line, f"{panel}.js uses {sink}: {stripped[:80]}"


@pytest.mark.parametrize("panel", PANELS)
def test_no_panel_hardcodes_a_raw_href(panel):
    """URLs come from ATS feeds. They go through link()/safeUrl, not href:."""
    src = (STATIC / "panels" / f"{panel}.js").read_text(encoding="utf-8")
    assert "href:" not in src, (
        f"{panel}.js sets href directly; use link() so the scheme is checked")


def test_the_ui_loads_nothing_from_the_network():
    """No CDN, no web font. The CSP forbids it, and this catches it earlier."""
    for f in [STATIC / "index.html", STATIC / "app.css", STATIC / "app.js"] + \
             list((STATIC / "panels").glob("*.js")):
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith(("*", "//", "/*", "#")):
                continue
            for tag in ("src=\"http", "src='http", "href=\"http", "@import url(http"):
                assert tag not in line, f"{f.name} loads a remote resource: {line.strip()[:80]}"
