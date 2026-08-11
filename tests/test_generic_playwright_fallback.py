"""Tests for the scrape_generic Playwright fallback.

Driven by the 8/10 headless-render audit: 401 stored careers pages render
their board client-side. The old fallback missed them three ways — it gave
up when the static GET was bot-blocked (never reaching Playwright), waited
on networkidle (flakes on pages with analytics beacons), and read only the
main document (embedded boards live in iframes).
"""
import job_scraper

JOB_HTML = """
<div class="job-listing">
  <h3>Senior Backend Engineer</h3>
  <a href="/jobs/senior-backend">Apply</a>
  <span class="location">Austin, TX</span>
</div>
"""

IFRAME_JOB_HTML = """
<div class="job-listing">
  <h3>Staff Product Designer</h3>
  <a href="https://example.com/jobs/staff-designer">Apply</a>
</div>
"""


def test_fallback_runs_when_static_get_is_blocked(monkeypatch):
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, **k: None)
    monkeypatch.setattr(job_scraper, "_render_careers_page", lambda url: [JOB_HTML])
    jobs = job_scraper.scrape_generic("https://example.com/careers")
    assert [j["title"] for j in jobs] == ["Senior Backend Engineer"]
    assert jobs[0]["url"] == "https://example.com/jobs/senior-backend"


def test_fallback_merges_iframe_html_and_dedups(monkeypatch):
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, **k: None)
    monkeypatch.setattr(
        job_scraper, "_render_careers_page",
        lambda url: [JOB_HTML, IFRAME_JOB_HTML, IFRAME_JOB_HTML],
    )
    jobs = job_scraper.scrape_generic("https://example.com/careers")
    assert sorted(j["title"] for j in jobs) == [
        "Senior Backend Engineer", "Staff Product Designer",
    ]


def test_no_fallback_when_static_html_has_jobs(monkeypatch):
    class _FakeResp:
        text = JOB_HTML

    def boom(url):
        raise AssertionError("fallback must not run when static parse found jobs")

    monkeypatch.setattr(job_scraper, "safe_get", lambda url, **k: _FakeResp())
    monkeypatch.setattr(job_scraper, "_render_careers_page", boom)
    jobs = job_scraper.scrape_generic("https://example.com/careers")
    assert [j["title"] for j in jobs] == ["Senior Backend Engineer"]


def test_render_failure_degrades_to_empty(monkeypatch):
    def boom(url):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(job_scraper, "safe_get", lambda url, **k: None)
    monkeypatch.setattr(job_scraper, "_render_careers_page", boom)
    assert job_scraper.scrape_generic("https://example.com/careers") == []
