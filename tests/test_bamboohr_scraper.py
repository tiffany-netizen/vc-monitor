"""Tests for the BambooHR scraper.

Driven by the 8/6 failing-links audit: 31 companies store a BambooHR board
that fell through to the generic scraper and returned nothing. The live
/careers/list JSON also distinguishes a genuinely empty board
(meta.totalCount == 0, e.g. conduitfinancial) from a broken one — the
generic scraper could not tell those apart.
"""
import job_scraper


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


SAMPLE = {
    "meta": {"totalCount": 2},
    "result": [
        {
            "id": "25",
            "jobOpeningName": "Diagnostics Engineer",
            "employmentStatusLabel": "Full-Time",
            "location": {"city": "SF Bay Area", "state": "California"},
            "isRemote": None,
        },
        {
            "id": "31",
            "jobOpeningName": "Field Support Engineer",
            "employmentStatusLabel": "Full-Time",
            "location": {},
            "isRemote": True,
        },
    ],
}


def test_scrape_bamboohr_maps_fields(monkeypatch):
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, **k: _FakeResp(SAMPLE))
    jobs = job_scraper.scrape_bamboohr("nexthopai")
    assert len(jobs) == 2
    first = jobs[0]
    assert first["title"] == "Diagnostics Engineer"
    assert first["location"] == "SF Bay Area, California"
    assert first["url"] == "https://nexthopai.bamboohr.com/careers/25"
    # remote job with no city/state gets a usable location
    assert jobs[1]["location"] == "Remote"


def test_scrape_bamboohr_empty_board_returns_no_jobs(monkeypatch):
    monkeypatch.setattr(
        job_scraper, "safe_get",
        lambda url, **k: _FakeResp({"meta": {"totalCount": 0}, "result": []}),
    )
    assert job_scraper.scrape_bamboohr("conduitfinancial") == []


def test_get_jobs_for_company_routes_bamboohr(monkeypatch):
    seen = {}

    def fake(slug):
        seen["slug"] = slug
        return [{"title": "x", "location": "", "url": "u", "salary_text": ""}]

    monkeypatch.setattr(job_scraper, "scrape_bamboohr", fake)
    jobs = job_scraper.get_jobs_for_company(
        {"ats_type": "bamboohr", "ats_slug": "nexthopai", "careers_url": "https://nexthop.ai/join-us/"}
    )
    assert seen["slug"] == "nexthopai"
    assert len(jobs) == 1
