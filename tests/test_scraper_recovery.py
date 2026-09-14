"""Tests for the 2026-09-14 throughput-review fixes: salary gate on range maximum,
dead-board strike-out for re-discovery, and Playwright-backed careers discovery."""

import pytest

import job_scraper


# ----------------------------------------------------------------
# Salary gate qualifies on the range maximum
# ----------------------------------------------------------------

def test_salary_qualifies_when_range_maximum_reaches_the_bar():
    # Measured 2026-09-14: demanding the range MINIMUM clear $200K dropped 27% of
    # genuine target roles. A band that reaches the bar at its top qualifies.
    assert job_scraper.salary_qualifies("$175,000 - $200,000")
    assert job_scraper.salary_qualifies("$194,000 - $227,000")
    assert job_scraper.salary_qualifies("$100K – $200K • Offers Equity")


def test_salary_qualifies_still_rejects_bands_entirely_below_the_bar():
    assert not job_scraper.salary_qualifies("$155,000 – $170,000")
    assert not job_scraper.salary_qualifies("$150,000")
    assert job_scraper.salary_qualifies("$250,000")
    assert job_scraper.salary_qualifies("")  # no salary posted -> passes


# ----------------------------------------------------------------
# Dead careers URLs get cleared for re-discovery
# ----------------------------------------------------------------

def _co(**kw):
    base = {"id": 42, "careers_url": "https://boards.greenhouse.io/acme",
            "ats_type": None, "ats_slug": None}
    base.update(kw)
    return base


def test_dead_board_live_url_records_nothing(monkeypatch):
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url, timeout=8: True)
    patches = []
    monkeypatch.setattr(job_scraper, "sb_patch", lambda t, f, d: patches.append(d) or True)
    job_scraper._record_dead_board("companies", _co(), "Acme", "https://boards.greenhouse.io/acme")
    assert patches == []


def test_dead_board_first_strike_only_increments(monkeypatch):
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url, timeout=8: False)
    monkeypatch.setattr(job_scraper, "sb_get",
                        lambda t, p: [{"careers_fail_streak": 0}])
    patches = []
    monkeypatch.setattr(job_scraper, "sb_patch", lambda t, f, d: patches.append(d) or True)
    job_scraper._record_dead_board("companies", _co(), "Acme", "https://boards.greenhouse.io/acme")
    assert patches == [{"careers_fail_streak": 1}]


def test_dead_board_third_strike_clears_url_for_rediscovery(monkeypatch):
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url, timeout=8: False)
    monkeypatch.setattr(job_scraper, "sb_get",
                        lambda t, p: [{"careers_fail_streak": 2}])
    patches = []
    monkeypatch.setattr(job_scraper, "sb_patch", lambda t, f, d: patches.append(d) or True)
    job_scraper._record_dead_board("companies", _co(), "Acme", "https://boards.greenhouse.io/acme")
    assert patches == [{"careers_url": None, "ats_type": None, "ats_slug": None,
                        "careers_fail_streak": 0}]


def test_dead_board_probes_the_ats_api_not_the_stored_page():
    # A company page can stay alive while its Greenhouse/Lever slug dies — the probe
    # must hit the endpoint that actually feeds jobs.
    co = _co(ats_type="greenhouse", ats_slug="acme", careers_url="https://acme.com/careers")
    assert "boards-api.greenhouse.io/v1/boards/acme" in job_scraper._board_probe_url(co)
    co = _co(ats_type="lever", ats_slug="acme", careers_url="https://acme.com/careers")
    assert "api.lever.co/v0/postings/acme" in job_scraper._board_probe_url(co)
    co = _co(ats_type=None, ats_slug=None, careers_url="https://acme.com/careers")
    assert job_scraper._board_probe_url(co) == "https://acme.com/careers"


# ----------------------------------------------------------------
# Discovery: careers link extraction + Playwright fallback
# ----------------------------------------------------------------

def test_careers_link_extracted_from_rendered_html():
    from bs4 import BeautifulSoup
    html = '<nav><a href="/about">About</a><a href="/company/careers">Careers</a></nav>'
    soup = BeautifulSoup(html, "lxml")
    hit = job_scraper._careers_link_from_soup(soup, "https://acme.com")
    assert hit == "https://acme.com/company/careers"


def test_careers_link_skips_social_profiles():
    from bs4 import BeautifulSoup
    html = '<a href="https://linkedin.com/company/acme/jobs">Jobs</a>'
    soup = BeautifulSoup(html, "lxml")
    assert job_scraper._careers_link_from_soup(soup, "https://acme.com") is None


def test_discovery_falls_back_to_playwright_when_static_fetch_is_blocked(monkeypatch):
    # Bot-blocked site: every static request fails, but the rendered homepage
    # exposes a Greenhouse board. Discovery should return the direct board URL.
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=15: None)
    monkeypatch.setattr(job_scraper, "_render_careers_page",
                        lambda url: ['<a href="https://boards.greenhouse.io/acme">Jobs</a>'])
    assert job_scraper.find_careers_url("acme.com") == "https://boards.greenhouse.io/acme"
