"""Greenhouse null location must not crash the scrape.

Run 37524335070 died on a Built posting whose location key was JSON null.
dict.get("location", "") returns None in that case, and .strip() raised.
"""
import logging

import job_scraper


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_greenhouse_null_and_missing_location_normalize_to_blank(monkeypatch):
    payload = {
        "jobs": [
            {
                "title": "VP of Operations",
                "absolute_url": "https://boards.greenhouse.io/built/jobs/1",
                "offices": None,
                "location": None,
                "metadata": None,
                "content": None,
            },
            {
                "title": "Chief of Staff",
                "absolute_url": "https://boards.greenhouse.io/built/jobs/2",
                "metadata": [],
                "content": "",
            },
            {
                "title": None,
                "absolute_url": None,
                "offices": [{"name": None}, None],
                "location": {"name": None},
                "metadata": [{"name": None, "value": None}],
                "content": "",
            },
        ]
    }
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=15: _FakeResp(payload))

    jobs = job_scraper.scrape_greenhouse("built")

    assert [j["location"] for j in jobs] == ["", "", ""]
    assert jobs[0]["title"] == "VP of Operations"
    assert jobs[0]["url"] == "https://boards.greenhouse.io/built/jobs/1"
    assert jobs[0]["salary_text"] == ""
    assert jobs[2]["title"] == ""
    assert jobs[2]["url"] == ""
    assert all(isinstance(j[field], str) for j in jobs for field in ("title", "location", "url", "salary_text"))


def test_lever_null_location_list_normalizes_to_blank(monkeypatch):
    payload = [{
        "text": None,
        "hostedUrl": None,
        "categories": {"location": None, "allLocations": [None]},
        "lists": None,
        "descriptionPlain": None,
    }]
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=15: _FakeResp(payload))

    jobs = job_scraper.scrape_lever("built")

    assert jobs == [{"title": "", "location": "", "url": "", "salary_text": ""}]


def test_ashby_null_location_and_title_normalize_to_blank(monkeypatch):
    class _Post:
        def json(self):
            return {"data": {"jobBoard": {"jobPostings": [
                {"id": "1", "title": "VP of Finance", "locationName": None, "compensationTierSummary": None},
                {"id": None, "title": None, "locationName": "Austin, TX"},
            ]}}}

    monkeypatch.setattr(job_scraper.requests, "post", lambda *a, **k: _Post())

    jobs = job_scraper.scrape_ashby("built")

    assert jobs[0]["location"] == ""
    assert jobs[0]["salary_text"] == ""
    assert jobs[0]["title"] == "VP of Finance"
    assert jobs[1]["title"] == ""
    assert jobs[1]["location"] == "Austin, TX"


def _company(company_id, name="Built"):
    return {
        "id": company_id,
        "name": name,
        "website": "built.com",
        "careers_url": "https://boards.greenhouse.io/built",
        "ats_type": "greenhouse",
        "ats_slug": "built",
    }


def _install_scrape_fakes(monkeypatch, companies, get_jobs, inserts, *, live=None):
    monkeypatch.setattr(job_scraper.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(job_scraper, "sb_patch_where", lambda table, params, data: 0)
    monkeypatch.setattr(job_scraper, "sb_get", lambda table, params, limit=1000: companies if table == "companies" else [])
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", get_jobs)
    monkeypatch.setattr(
        job_scraper, "url_is_live",
        live if live is not None else (lambda url, timeout=8: True),
    )


def test_null_location_jobs_are_saved(monkeypatch):
    inserts = []
    jobs = [
        {
            "title": "VP of Operations",
            "location": None,
            "salary_text": "$250,000",
            "url": "https://boards.greenhouse.io/built/jobs/1",
        },
        {
            "title": "Chief of Staff",
            "salary_text": None,
            "url": "https://boards.greenhouse.io/built/jobs/2",
        },
    ]
    _install_scrape_fakes(monkeypatch, [_company(7)], lambda co: jobs, inserts)

    job_scraper.scrape_jobs("companies")

    saved = [row for table, row in inserts if table == "jobs"]
    assert [row["title"] for row in saved] == ["VP of Operations", "Chief of Staff"]
    assert [row["location"] for row in saved] == ["", ""]
    assert saved[1]["salary_text"] == ""


def test_bad_job_is_skipped_and_the_next_job_is_saved(monkeypatch, caplog):
    inserts = []

    def live(url, timeout=8):
        if url.endswith("/bad"):
            raise RuntimeError("null location blew up")
        return True

    jobs = [
        {
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$250,000",
            "url": "https://boards.greenhouse.io/built/jobs/bad",
        },
        {
            "title": "Chief of Staff",
            "location": "New York, NY",
            "salary_text": "$250,000",
            "url": "https://boards.greenhouse.io/built/jobs/good",
        },
    ]
    _install_scrape_fakes(monkeypatch, [_company(7)], lambda co: jobs, inserts, live=live)

    with caplog.at_level(logging.INFO):
        job_scraper.scrape_jobs("companies")

    saved = [row for table, row in inserts if table == "jobs"]
    assert [row["url"] for row in saved] == ["https://boards.greenhouse.io/built/jobs/good"]
    assert "Built: skipped job title='VP of Operations'" in caplog.text
    assert "https://boards.greenhouse.io/built/jobs/bad" in caplog.text
    assert "RuntimeError: null location blew up" in caplog.text
    assert "1 job(s) skipped after an error" in caplog.text


def test_company_scrape_error_does_not_stop_the_next_company(monkeypatch, caplog):
    inserts = []

    def get_jobs(co):
        if co["id"] == 1:
            raise RuntimeError("board exploded")
        return [{
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$250,000",
            "url": "https://boards.greenhouse.io/ok/jobs/1",
        }]

    _install_scrape_fakes(
        monkeypatch,
        [_company(1, "Built"), _company(2, "OkCo")],
        get_jobs,
        inserts,
    )

    with caplog.at_level(logging.INFO):
        job_scraper.scrape_jobs("companies")

    saved = [row for table, row in inserts if table == "jobs"]
    assert [row["company_name"] for row in saved] == ["OkCo"]
    assert "Built: scrape failed (RuntimeError: board exploded)" in caplog.text
    assert "1 company scrape(s) failed" in caplog.text
