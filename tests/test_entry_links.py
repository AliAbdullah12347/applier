"""Links from the original resume must survive tailoring.

The original carried eight. Four are in the header and always rendered. The
other four hung off individual entries -- a GitHub repository, two live
demos, and the product site on a *role* -- and every tailored resume dropped
all four.

Two separate causes, which is why it was invisible:

* A project's `url` was parsed, stored on the role, and passed to the
  template, which never referenced it. Nothing was lost on the way; the last
  step simply did not print it.

* A role could not hold a `url` at all. The importer built a different shape
  for non-project sections and `url` was not in it, so the one link on an
  experience entry had nowhere to live even in principle.
"""

from __future__ import annotations

import re

import pytest

from applier.tailor.master import build_bank
from applier.tailor.render import link_label, render_tex, tex_url

MASTER = """## Experience

### Engineer — Northwind
id: exp_role
org: Northwind Labs
url: https://northwind.example.com/
dates: Jan 2026 -- May 2026
tags: python

- Shipped the thing that needed shipping.

### Engineer — Contoso
id: exp_plain
org: Contoso
dates: Jan 2025 -- May 2025
tags: python

- Shipped another thing.

## Projects

### Widget — A Tool
id: project_widget
stack: Python
url: https://github.com/example/widget
dates: 2026

- Built a widget.

### Gadget — Another Tool
id: project_gadget
stack: Python
dates: 2026

- Built a gadget.
"""


@pytest.fixture
def roles(tmp_path):
    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    from applier.tailor.bank import Bank
    return Bank(tmp_path).roles


# --------------------------------------------------------------------------- #
# the importer
# --------------------------------------------------------------------------- #
def test_a_project_keeps_its_url(roles):
    assert roles["project_widget"]["url"] == "https://github.com/example/widget"


def test_a_role_keeps_its_url(roles):
    """The shape that had no `url` key at all, so the link had nowhere to go."""
    assert roles["exp_role"]["url"] == "https://northwind.example.com/"


def test_an_entry_without_a_url_gets_an_empty_one(roles):
    assert roles["exp_plain"]["url"] == ""
    assert roles["project_gadget"]["url"] == ""


# --------------------------------------------------------------------------- #
# the escaping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expect", [
    ("https://github.com/a/b", "https://github.com/a/b"),
    ("https://x.example/a?b=1&c=2", r"https://x.example/a?b=1\&c=2"),
    ("https://x.example/a#frag", r"https://x.example/a\#frag"),
    ("https://x.example/100%ok", r"https://x.example/100\%ok"),
    # An underscore and a tilde are ordinary characters to hyperref, and
    # tex_escape would turn them into commands.
    ("https://x.example/a_b~c", "https://x.example/a_b~c"),
])
def test_a_url_is_escaped_for_href_not_for_prose(url, expect):
    assert tex_url(url) == expect


@pytest.mark.parametrize("bad", [
    "https://x.example/{a}", "https://x.example/a\\b", "https://x.example/a b",
    "https://x.example/a\nb", "", None,
])
def test_a_url_that_cannot_be_linked_costs_the_link_not_the_compile(bad):
    """A brace or a backslash would end the argument or start a command.

    Dropping the link leaves a resume with one fewer link. Emitting it leaves
    a resume that does not build, which is the worse of the two.
    """
    assert tex_url(bad) == ""


@pytest.mark.parametrize("url,label", [
    ("https://github.com/a/b", "GitHub"),
    ("https://gitlab.com/a/b", "GitLab"),
    ("https://thing.vercel.app", "Live Demo"),
    ("https://thing.netlify.app", "Live Demo"),
    ("https://www.agentsofinfluencegame.com/", "agentsofinfluencegame.com"),
])
def test_the_link_says_something_shorter_than_the_address(url, label):
    assert link_label(url) == label


def test_an_explicit_label_wins():
    assert link_label("https://github.com/a/b", "Live Demo") == "Live Demo"


# --------------------------------------------------------------------------- #
# the template
# --------------------------------------------------------------------------- #
def _tex(**ctx) -> str:
    from applier.config import resolve_asset

    base = {"identity": {"full_name": "A Candidate", "phone": "",
                         "email": "a@example.com", "linkedin": "", "github": "",
                         "website": "", "linkedin_display": "",
                         "github_display": "", "website_display": ""},
            "education": [], "education_atoms": [], "experience": [],
            "projects": [], "leadership": [], "skills": [],
            "item_sep": 1, "font_pt": 10, "margin_in": 0.5}
    base.update(ctx)
    return render_tex(resolve_asset("templates/resume/classic.tex.j2"), base)


def test_a_project_url_reaches_the_page():
    out = _tex(projects=[{"name": "Widget", "stack": "Python", "points": ["did it"],
                          "url": "https://github.com/example/widget", "link_text": ""}])
    assert r"\href{https://github.com/example/widget}" in out
    assert "GitHub" in out


def test_a_role_links_its_organisation():
    """Appending a label printed the organisation's name twice.

    The original resume made the organisation itself the link, which is also
    what a reader expects on a role.
    """
    out = _tex(experience=[{"title": "Engineer", "org": "Northwind Labs",
                            "location": "", "dates": "2026", "points": ["did it"],
                            "url": "https://northwind.example.com/", "link_text": ""}])
    assert r"\href{https://northwind.example.com/}{\underline{Northwind Labs}}" in out


def test_an_entry_without_a_url_prints_no_link():
    out = _tex(experience=[{"title": "Engineer", "org": "Contoso", "location": "",
                            "dates": "2026", "points": ["did it"],
                            "url": "", "link_text": ""}])
    # The header always carries its own links, so count what wraps the org
    # rather than what is on the page.
    assert "Contoso" in out
    assert r"\underline{Contoso}" not in out


def test_an_unlinkable_url_still_prints_the_organisation():
    out = _tex(experience=[{"title": "Engineer", "org": "Contoso", "location": "",
                            "dates": "2026", "points": ["did it"],
                            "url": "https://x.example/{bad}", "link_text": ""}])
    assert "Contoso" in out
    assert "{bad}" not in out


# --------------------------------------------------------------------------- #
# nothing silently stops carrying a link
# --------------------------------------------------------------------------- #
def test_every_url_in_the_master_file_reaches_a_role(tmp_path):
    """The defect was a link going missing between two steps, so the test
    walks the same route: master file -> bank -> rendered page."""
    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    from applier.tailor.bank import Bank

    declared = set(re.findall(r"^url:\s*(\S+)", MASTER, re.M))
    carried = {str(r.get("url")) for r in Bank(tmp_path).roles.values() if r.get("url")}
    assert declared == carried, f"lost between the file and the bank: {declared - carried}"
