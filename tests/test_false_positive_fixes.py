"""
Tests for the scraper false-positive fixes (changes 1-4).

These target the real failures seen in production sample rows:
  1. "Ghost" jobs: a row whose only URL is the careers page (m3ter, Botrista).
  2. Junk pages scraped as jobs: team/people profiles and marketing pages
     (Phorest employee profiles, Consero "2024 CFO Survey").
  3. Title/location mashups ("Head of Business Operations San Francisco, ...").
  4. Dead links inserted as new jobs (knownwell, BackOps 404s).
"""
from bs4 import BeautifulSoup

import job_scraper


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ── Change 1: drop "ghost" jobs (no per-job link / link == careers page) ──────

def test_extract_drops_jobs_without_a_real_link():
    html = """
      <a class="job-listing" href="/jobs/123-cfo">Chief Financial Officer</a>
      <div class="job-item">Head of Finance</div>            <!-- no link: ghost -->
      <a class="job-item" href="/careers">View all roles</a> <!-- link == careers page -->
    """
    jobs = job_scraper._extract_jobs_from_soup(_soup(html), "https://acme.com/careers")
    titles = {j["title"] for j in jobs}
    assert "Chief Financial Officer" in titles
    assert "Head of Finance" not in titles          # ghost (no href) dropped
    assert "View all roles" not in titles           # url == careers page dropped
    # the kept job points at its own URL, not the careers page
    cfo = next(j for j in jobs if j["title"] == "Chief Financial Officer")
    assert cfo["url"] == "https://acme.com/jobs/123-cfo"


# ── Change 2: skip team/people profiles and marketing pages ──────────────────

def test_extract_skips_people_and_survey_urls():
    html = """
      <a class="job-item" href="/en-GB/people/1002888-ana-kelly">Ana Kelly Financial Controller</a>
      <a class="job-item" href="/2024-cfo-survey/">2024 CFO Survey</a>
      <a class="job-item" href="/jobs/456-controller">Controller</a>
    """
    jobs = job_scraper._extract_jobs_from_soup(_soup(html), "https://phorest.com/careers")
    titles = {j["title"] for j in jobs}
    assert titles == {"Controller"}


# ── Change 3: title is the first line only (strip appended location) ──────────

def test_extract_title_takes_first_line_not_location_mashup():
    html = """
      <a class="job-posting" href="/jobs/789">Head of Business Operations<br/>San Francisco, United States</a>
    """
    jobs = job_scraper._extract_jobs_from_soup(_soup(html), "https://backops.ai/careers")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Head of Business Operations"


# ── Change 4: url_is_live + don't insert new jobs whose link is dead ─────────

class _FakeResp:
    def __init__(self, status):
        self.status_code = status


def test_url_is_live_false_only_on_gone(monkeypatch):
    monkeypatch.setattr(job_scraper.requests, "head", lambda *a, **k: _FakeResp(404))
    assert job_scraper.url_is_live("https://x.com/dead") is False

    monkeypatch.setattr(job_scraper.requests, "head", lambda *a, **k: _FakeResp(410))
    assert job_scraper.url_is_live("https://x.com/gone") is False

    monkeypatch.setattr(job_scraper.requests, "head", lambda *a, **k: _FakeResp(200))
    assert job_scraper.url_is_live("https://x.com/live") is True


def test_url_is_live_fails_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network blocked")
    monkeypatch.setattr(job_scraper.requests, "head", boom)
    # A real job behind a bot-block must NOT be dropped.
    assert job_scraper.url_is_live("https://x.com/maybe") is True


def test_scrape_jobs_skips_inserting_dead_new_job(monkeypatch):
    company = {
        "id": 11, "name": "BackOps", "website": "backops.ai",
        "careers_url": "https://jobs.gem.com/backops-ai", "ats_type": "generic", "ats_slug": None,
    }
    dead_job = {
        "title": "Head of Business Operations",
        "location": "Remote - US",
        "salary_text": "$250,000",
        "url": "https://jobs.gem.com/backops-ai/DEADID",
    }
    inserts = []

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and "select" in params:
            return [company]
        return []  # job url lookup -> not existing

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda t, d: inserts.append((t, d)) or True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda *a, **k: True)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda co: [dead_job])
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url: False)

    job_scraper.scrape_jobs("companies")
    assert inserts == []  # dead link must not be inserted
