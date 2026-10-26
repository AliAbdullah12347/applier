"""Finding people from a job link, and refusing to invent them.

Outreach is the only part of this system whose output is a message to a
stranger signed with Ali's real name. That makes a false positive here
categorically worse than a miss: a thin list costs an opportunity, a wrong
list costs a reputation, and it cannot be taken back.

Two near-misses are recorded below as tests.

**A domain guessed from a company name.** "xAI" resolved to xai.com, which
belongs to a company called CTR Limited that holds the domain. The check that
accepted it read the page for the company's name and found "XAI.COM" in the
title -- the domain vouching for itself. Every parking page on the internet
passes that test.

**Names read out of markup.** Flattening a team page to text and looking for
two capitalised words near a job-title word produced "Blog Trust", "Careers
News" and "Center Contact" from a navigation bar. Tightening it produced
"Chief Executive Officer" and "Fundraising Operations" from a staff directory.
Each round removed one class of nonsense and revealed the next, which is the
signal that the approach is wrong rather than unfinished.

No test here touches the network.
"""

from __future__ import annotations

import pytest

from applier.outreach import forjob as FJ
from applier.outreach.people import people_on_page


# --------------------------------------------------------------------------- #
# whose site is this
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host,ats", [
    ("job-boards.greenhouse.io", True),
    ("jobs.lever.co", True),
    ("jobs.ashbyhq.com", True),
    ("acme.myworkdayjobs.com", True),
    ("simplify.jobs", True),
    ("linkedin.com", True),
    ("ramp.com", False),
    ("wikimediafoundation.org", False),
    ("lifeattiktok.com", False),
])
def test_an_ats_host_is_not_the_employer(host, ats):
    assert FJ.is_ats_host(host) is ats


@pytest.mark.parametrize("host,expected", [
    ("jobs.bytedance.com", "bytedance.com"),
    ("careers.google.com", "google.com"),
    ("www.ramp.com", "ramp.com"),
    ("ramp.com", "ramp.com"),
    ("engineering.example.com", "engineering.example.com"),   # not a careers sub
])
def test_a_careers_subdomain_resolves_to_the_employer(host, expected):
    assert FJ._registrable(host) == expected


PARKED = """<html><head><title>XAI.COM &mdash; CTR LIMITED</title></head>
<body><h1>XAI.COM</h1><p>This domain is held by CTR Limited.</p></body></html>"""

REAL = """<html><head><title>xAI</title>
<meta property="og:site_name" content="xAI"></head>
<body><p>We are building artificial intelligence to accelerate discovery.</p></body></html>"""


def test_a_domain_holder_page_does_not_vouch_for_itself():
    """The exact false positive: the page's only mention of the company is the
    domain name printed in its own title."""
    assert FJ._looks_like("xAI", PARKED, "xai.com", strict=True) is False


def test_a_real_site_declaring_itself_is_accepted():
    assert FJ._looks_like("xAI", REAL, "xai.com", strict=True) is True


def test_body_copy_is_enough_only_when_something_already_pointed_here():
    """A domain the posting names has already been corroborated once."""
    body = "<html><body><p>Ramp is a corporate card company.</p></body></html>"
    assert FJ._looks_like("Ramp", body, "ramp.com", strict=False) is True
    assert FJ._looks_like("Ramp", body, "ramp.com", strict=True) is False


def test_a_company_name_too_short_to_identify_is_refused():
    page = "<html><head><title>Go</title></head><body>Go</body></html>"
    assert FJ._looks_like("Go", page, "go.com", strict=True) is False


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #
NAV_PAGE = """<html><body>
<nav><a href="/blog">Blog</a><a href="/trust">Trust</a>
     <a href="/careers">Careers</a><a href="/news">News</a>
     <a href="/company">Company</a><a href="/about">About</a></nav>
<main><h2>Our leadership team</h2>
<p>Our managers lead research across early literacy and assessment.</p></main>
<footer>Contact Center &middot; Sales Support &middot; United States</footer>
</body></html>"""


def test_a_navigation_bar_is_not_a_list_of_people():
    """Blog + Trust were two adjacent menu links, read as one person."""
    found = [name for name, _ in people_on_page(NAV_PAGE, heuristic=True)]
    for invented in ("Blog Trust", "Careers News", "Center Contact",
                     "Company About", "Sales Support", "United States"):
        assert invented not in found, f"invented a person: {invented}"


@pytest.mark.parametrize("title", [
    "Chief Executive Officer", "Deputy Chief", "Fundraising Operations",
    "Internal Communications", "Wikimedia Enterprise", "Early Literacy",
    "Research Hub", "Senior Engineer", "Head Of",
])
def test_a_job_title_is_never_a_name(title):
    from applier.outreach.people import _plausible_name
    assert _plausible_name(title) == "", f"{title!r} was accepted as a person"


@pytest.mark.parametrize("name", [
    "Dana Okafor", "Ravi Shankar", "Mukunda Modell", "Alex Ghirelli",
    "Mary-Jane O'Connell", "Jean Luc Picard",
])
def test_a_real_name_still_passes(name):
    from applier.outreach.people import _plausible_name
    assert _plausible_name(name) == name


LD_PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","employee":[
 {"@type":"Person","name":"Dana Okafor","jobTitle":"Staff Engineer"},
 {"@type":"Person","name":"Ravi Shankar","jobTitle":"Head of Recruiting"}]}
</script></head><body>Blog Trust Careers News</body></html>"""


def test_structured_data_is_read_and_the_navigation_is_not():
    people = people_on_page(LD_PAGE)
    assert people == [("Dana Okafor", "Staff Engineer"),
                      ("Ravi Shankar", "Head of Recruiting")]


def test_guessing_is_off_by_default():
    """The default path must not produce anyone from markup alone."""
    assert people_on_page(NAV_PAGE) == []


def test_malformed_html_does_not_raise():
    for junk in ("", "<<<>>", "<html><body><div>unclosed", "\x00\x01"):
        assert isinstance(people_on_page(junk), list)


# --------------------------------------------------------------------------- #
# the posting's own contact
# --------------------------------------------------------------------------- #
def test_a_published_contact_address_is_used():
    job = {"company": "Example", "title": "Intern",
           "url": "https://job-boards.greenhouse.io/example/jobs/1",
           "description": "Questions? Write to jane.doe@example.com."}
    people = list(FJ.from_posting(job, "example.com"))
    assert len(people) == 1
    assert people[0].email == "jane.doe@example.com"
    assert people[0].hook_source_url == job["url"]


@pytest.mark.parametrize("addr", [
    "noreply@example.com", "privacy@example.com", "legal@example.com",
    "careers@example.com",          # a role address, not a person
    "jane@other.example",               # a different company entirely
])
def test_an_address_that_is_not_a_person_is_skipped(addr):
    job = {"company": "Example", "title": "Intern", "url": "https://x/y",
           "description": f"Contact {addr} with questions."}
    assert list(FJ.from_posting(job, "example.com")) == []


# --------------------------------------------------------------------------- #
# the rule that makes the rest safe
# --------------------------------------------------------------------------- #
def test_a_guessed_domain_cannot_produce_an_address(monkeypatch):
    """The whole safety property, end to end.

    If the domain was guessed, every person found through it comes back with
    no email. draft.py refuses a contact with no channel, so nothing can be
    written to them until a human confirms the domain.
    """
    from applier.outreach.discover import Person

    monkeypatch.setattr(FJ, "resolve_employer",
                        lambda *a, **k: ("maybe-wrong.example", "guessed"))
    monkeypatch.setattr(FJ, "from_posting", lambda *a, **k: iter(()))
    monkeypatch.setattr(FJ, "from_company_github", lambda *a, **k: iter(()))
    monkeypatch.setattr(FJ, "from_company_site", lambda *a, **k: iter([
        Person(name="Dana Okafor", domain="maybe-wrong.example",
               email="dana@maybe-wrong.example", role="engineer",
               profile_url="https://maybe-wrong.example/team",
               hook_fact="listed on the team page",
               hook_source_url="https://maybe-wrong.example/team"),
    ]))

    people = FJ.people_for_job({"company": "Maybe", "title": "Intern"}, None, None)
    assert people, "the person should still be shown"
    assert all(not p.email for p in people), "an address survived a guessed domain"
    assert "unconfirmed" in people[0].notes.lower()


def test_a_trusted_domain_keeps_its_addresses(monkeypatch):
    from applier.outreach.discover import Person

    monkeypatch.setattr(FJ, "resolve_employer", lambda *a, **k: ("example.com", "posting"))
    monkeypatch.setattr(FJ, "from_posting", lambda *a, **k: iter(()))
    monkeypatch.setattr(FJ, "from_company_github", lambda *a, **k: iter(()))
    monkeypatch.setattr(FJ, "from_company_site", lambda *a, **k: iter([
        Person(name="Dana Okafor", domain="example.com", email="dana@example.com",
               role="engineer", profile_url="https://example.com/team",
               hook_fact="listed on the team page",
               hook_source_url="https://example.com/team"),
    ]))
    people = FJ.people_for_job({"company": "Example", "title": "Intern"}, None, None)
    assert people[0].email == "dana@example.com"


def test_people_for_job_never_raises(monkeypatch):
    """An unattended run must not die because a company has no website."""
    def boom(*a, **k):
        raise RuntimeError("network on fire")

    monkeypatch.setattr(FJ, "resolve_employer", boom)
    assert FJ.people_for_job({"company": "X", "title": "Y"}, None, None) == []


def test_ranking_puts_a_published_contact_first():
    from applier.outreach.discover import Person
    people = [
        Person(name="Some Engineer", role="engineer", email="e@example.com", source="github_org"),
        Person(name="Named Contact", role="named in the posting",
               email="n@example.com", source="posting"),
    ]
    ranked = FJ.rank(people, {"title": "Software Engineering Intern"})
    assert ranked[0].source == "posting"


def test_a_campus_posting_prefers_recruiters_over_engineers():
    from applier.outreach.discover import Person
    people = [
        Person(name="Some Engineer", role="software engineer", email="e@example.com"),
        Person(name="Campus Person", role="university recruiting lead", email="r@example.com"),
    ]
    ranked = FJ.rank(people, {"title": "Summer Associate, Campus Programme"})
    assert ranked[0].name == "Campus Person"
