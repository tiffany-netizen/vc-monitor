import sys

import pytest

import job_scraper
import vc_monitor


def test_title_location_salary_filters_basics():
    assert job_scraper.title_matches("Chief Financial Officer")
    assert not job_scraper.title_matches("Senior Software Engineer")

    assert job_scraper.is_us_location("Remote - United States")
    assert not job_scraper.is_us_location("London, UK")

    assert job_scraper.salary_qualifies("$250,000 - $320,000")
    assert not job_scraper.salary_qualifies("$150,000 - $190,000")


def test_scrape_jobs_companies_inserts_only_matching_roles(monkeypatch):
    fake_companies = [
        {
            "id": 11,
            "name": "Acme",
            "website": "acme.com",
            "careers_url": "https://acme.com/careers",
            "ats_type": "generic",
            "ats_slug": None,
        }
    ]
    fake_jobs = [
        {
            "title": "Chief Financial Officer",
            "location": "London, UK",
            "salary_text": "$300,000",
            "url": "https://acme.com/jobs/cfo-london",
        },
        {
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$250,000 - $280,000",
            "url": "https://acme.com/jobs/vp-ops-us",
        },
        {
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$140,000 - $180,000",
            "url": "https://acme.com/jobs/vp-ops-low",
        },
    ]

    inserts = []
    patches = []

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and "select" in params:
            return fake_companies
        if table == "jobs" and "url" in params:
            return []
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: patches.append((table, filters, data)) or True)

    job_scraper.scrape_jobs("companies")

    job_rows = [row for table, row in inserts if table == "jobs"]
    assert len(job_rows) == 1
    assert job_rows[0]["title"] == "VP of Operations"
    assert job_rows[0]["company_name"] == "Acme"
    assert job_rows[0]["dna_fit"] is True

    assert any(
        table == "companies" and filters == {"id": 11} and "last_scraped" in data
        for table, filters, data in patches
    )


def test_scrape_jobs_existing_url_updates_instead_of_inserting(monkeypatch):
    fake_companies = [
        {
            "id": 12,
            "name": "Acme",
            "website": "acme.com",
            "careers_url": "https://acme.com/careers",
            "ats_type": "generic",
            "ats_slug": None,
        }
    ]
    fake_jobs = [
        {
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$260,000",
            "url": "https://acme.com/jobs/vp-ops-us",
        }
    ]

    inserts = []
    patches = []

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and "select" in params:
            return fake_companies
        if table == "jobs" and params.get("url") == "eq.https://acme.com/jobs/vp-ops-us":
            return [{"url": "https://acme.com/jobs/vp-ops-us"}]
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: patches.append((table, filters, data)) or True)

    job_scraper.scrape_jobs("companies")

    assert not any(table == "jobs" and row.get("url") == "https://acme.com/jobs/vp-ops-us" for table, row in inserts)
    job_updates = [(filters, data) for table, filters, data in patches if table == "jobs"]
    assert len(job_updates) == 1
    assert job_updates[0][0] == {"url": "https://acme.com/jobs/vp-ops-us"}
    assert "last_seen" in job_updates[0][1]
    assert job_updates[0][1]["dna_fit"] is True


def test_scrape_jobs_vc_writes_to_vc_jobs_table(monkeypatch):
    fake_vc_companies = [
        {
            "id": 77,
            "company": "Portco One",
            "domain": "portco.one",
            "careers_url": "https://jobs.portco.one",
            "ats_type": "generic",
            "ats_slug": None,
            "vc_names": ["Example VC"],
        }
    ]
    fake_jobs = [
        {
            "title": "Head of Finance",
            "location": "San Francisco, CA",
            "salary_text": "$260,000",
            "url": "https://jobs.portco.one/hof",
        }
    ]

    inserts = []

    def fake_sb_get(table, params, limit=1000):
        if table == "vc_portfolio_companies" and "select" in params:
            return fake_vc_companies
        if table == "vc_jobs" and "url" in params:
            return []
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)

    job_scraper.scrape_jobs("vc")

    assert any(table == "vc_jobs" for table, _ in inserts)
    assert any(table == "jobs" for table, _ in inserts)


def test_scrape_jobs_vc_also_mirrors_into_jobs_for_dashboard(monkeypatch):
    fake_vc_companies = [
        {
            "id": 88,
            "company": "Portco Two",
            "domain": "portco.two",
            "careers_url": "https://jobs.portco.two",
            "ats_type": "generic",
            "ats_slug": None,
            "vc_names": ["Example VC"],
        }
    ]
    fake_jobs = [
        {
            "title": "Head of Finance",
            "location": "Remote - US",
            "salary_text": "$260,000",
            "url": "https://jobs.portco.two/hof",
        }
    ]

    inserts = []

    def fake_sb_get(table, params, limit=1000):
        if table == "vc_portfolio_companies" and "select" in params:
            return fake_vc_companies
        if table in {"vc_jobs", "jobs"} and "url" in params:
            return []
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)

    job_scraper.scrape_jobs("vc")

    assert any(table == "vc_jobs" for table, _ in inserts)
    assert any(table == "jobs" for table, _ in inserts)
    mirrored_jobs = [row for table, row in inserts if table == "jobs"]
    assert len(mirrored_jobs) == 1
    assert mirrored_jobs[0]["dna_fit"] is True


def test_scrape_jobs_vc_existing_mirror_updates_dna_fit(monkeypatch):
    fake_vc_companies = [
        {
            "id": 99,
            "company": "Portco Three",
            "domain": "portco.three",
            "careers_url": "https://jobs.portco.three",
            "ats_type": "generic",
            "ats_slug": None,
            "vc_names": ["Example VC"],
        }
    ]
    fake_jobs = [
        {
            "title": "Head of Finance",
            "location": "Remote - US",
            "salary_text": "$260,000",
            "url": "https://jobs.portco.three/hof",
        }
    ]

    patches = []

    def fake_sb_get(table, params, limit=1000):
        if table == "vc_portfolio_companies" and "select" in params:
            return fake_vc_companies
        if table == "vc_jobs" and "url" in params:
            return []
        if table == "jobs" and params.get("url") == "eq.https://jobs.portco.three/hof":
            return [{"url": "https://jobs.portco.three/hof"}]
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: patches.append((table, filters, data)) or True)

    job_scraper.scrape_jobs("vc")

    mirrored_updates = [(filters, data) for table, filters, data in patches if table == "jobs"]
    assert len(mirrored_updates) == 1
    assert mirrored_updates[0][0] == {"url": "https://jobs.portco.three/hof"}
    assert mirrored_updates[0][1]["dna_fit"] is True
    assert "last_seen" in mirrored_updates[0][1]


def test_vc_monitor_scan_all_jobs_sets_dna_fit_on_insert(monkeypatch):
    fake_companies = [
        {
            "id": 501,
            "company": "DualEntry",
            "stage": "Series A",
            "vc_names": ["Lightspeed"],
            "careers_url": "https://dualentry.com/careers",
            "ats_type": "generic",
            "ats_slug": None,
        }
    ]
    fake_jobs = [
        {
            "title": "Chief of Staff",
            "location": "New York, NY",
            "salary_text": "$250,000",
            "url": "https://dualentry.com/open-roles/chief-of-staff",
        }
    ]
    inserts = []
    updates = []

    class DummyResp:
        text = "ok"

        def raise_for_status(self):
            return None

        def json(self):
            return fake_companies

    monkeypatch.setattr(vc_monitor.requests, "get", lambda *args, **kwargs: DummyResp())
    monkeypatch.setattr(vc_monitor, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(vc_monitor.sb, "get_by_url", lambda url, table="vc_jobs": None)
    monkeypatch.setattr(vc_monitor.sb, "insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(vc_monitor.sb, "update", lambda table, filters, data: updates.append((table, filters, data)) or True)
    monkeypatch.setattr(vc_monitor.time, "sleep", lambda _: None)

    vc_monitor.scan_all_jobs()

    vc_job_rows = [row for table, row in inserts if table == "vc_jobs"]
    main_job_rows = [row for table, row in inserts if table == "jobs"]
    assert len(vc_job_rows) == 1
    assert len(main_job_rows) == 1
    assert main_job_rows[0]["dna_fit"] is True


def test_vc_monitor_scan_all_jobs_sets_dna_fit_on_existing_jobs(monkeypatch):
    fake_companies = [
        {
            "id": 502,
            "company": "Corgi Insurance",
            "stage": "Series A",
            "vc_names": ["Y Combinator"],
            "careers_url": "https://corgi.insure/careers",
            "ats_type": "generic",
            "ats_slug": None,
        }
    ]
    fake_jobs = [
        {
            "title": "Chief of Staff",
            "location": "Remote - US",
            "salary_text": "$240,000",
            "url": "https://corgi.insure/jobs/chief-of-staff",
        }
    ]
    updates = []

    class DummyResp:
        text = "ok"

        def raise_for_status(self):
            return None

        def json(self):
            return fake_companies

    monkeypatch.setattr(vc_monitor.requests, "get", lambda *args, **kwargs: DummyResp())
    monkeypatch.setattr(vc_monitor, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(vc_monitor.sb, "get_by_url", lambda url, table="vc_jobs": {"url": url})
    monkeypatch.setattr(vc_monitor.sb, "insert", lambda table, data: True)
    monkeypatch.setattr(vc_monitor.sb, "update", lambda table, filters, data: updates.append((table, filters, data)) or True)
    monkeypatch.setattr(vc_monitor.time, "sleep", lambda _: None)

    vc_monitor.scan_all_jobs()

    vc_updates = [(filters, data) for table, filters, data in updates if table == "vc_jobs"]
    main_updates = [(filters, data) for table, filters, data in updates if table == "jobs"]
    assert len(vc_updates) == 1
    assert len(main_updates) == 1
    assert vc_updates[0][0] == {"url": "https://corgi.insure/jobs/chief-of-staff"}
    assert main_updates[0][0] == {"url": "https://corgi.insure/jobs/chief-of-staff"}
    assert main_updates[0][1]["dna_fit"] is True


def test_vc_monitor_select_where_builds_filter_params(monkeypatch):
    captured = {}

    class DummyResp:
        text = "[]"

        def raise_for_status(self):
            return None

        def json(self):
            return []

    def fake_get(url, headers=None, params=None, timeout=15):
        captured["params"] = params
        return DummyResp()

    monkeypatch.setattr(vc_monitor.requests, "get", fake_get)

    client = vc_monitor.SupabaseClient(url="https://example.supabase.co/rest/v1", key="test")
    client.select_where(
        "vc_portfolio_companies",
        {"active": "eq.true", "status": "eq.approved"},
    )

    assert captured["params"]["active"] == "eq.true"
    assert captured["params"]["status"] == "eq.approved"


def test_sb_get_returns_empty_list_on_http_error(monkeypatch):
    class DummyResp:
        text = ""

        def raise_for_status(self):
            raise job_scraper.requests.HTTPError("503 Service Unavailable")

    monkeypatch.setattr(job_scraper.requests, "get", lambda *args, **kwargs: DummyResp())

    assert job_scraper.sb_get("companies", {"select": "id"}) == []


def test_sb_patch_returns_false_on_http_error(monkeypatch):
    class DummyResp:
        def raise_for_status(self):
            raise job_scraper.requests.HTTPError("500 Internal Server Error")

    monkeypatch.setattr(job_scraper.requests, "patch", lambda *args, **kwargs: DummyResp())

    assert job_scraper.sb_patch("companies", {"id": 1}, {"careers_url": "https://acme.com/careers"}) is False


def test_scrape_logs_supabase_write_failures(monkeypatch, caplog):
    fake_companies = [
        {
            "id": 19,
            "name": "Acme",
            "website": "acme.com",
            "careers_url": "https://acme.com/careers",
            "ats_type": "generic",
            "ats_slug": None,
        }
    ]
    fake_jobs = [
        {
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$250,000",
            "url": "https://acme.com/jobs/vp-ops",
        }
    ]

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and "select" in params:
            return fake_companies
        if table == "jobs" and "url" in params:
            return []
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: False)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)

    with caplog.at_level("ERROR"):
        job_scraper.scrape_jobs("companies")

    assert "Supabase INSERT failed for jobs: company=Acme title=VP of Operations url=https://acme.com/jobs/vp-ops" in caplog.text


def test_discover_then_scrape_smoke_flow(monkeypatch):
    state = {
        "companies": [
            {
                "id": 31,
                "name": "Acme",
                "website": "acme.com",
                "careers_url": None,
                "ats_type": None,
                "ats_slug": None,
            }
        ],
        "jobs": [],
    }

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and params.get("careers_url") == "is.null":
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "website": row["website"],
                }
                for row in state["companies"]
                if row["careers_url"] is None
            ]
        if table == "companies" and params.get("careers_url") == "neq.none":
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "website": row["website"],
                    "careers_url": row["careers_url"],
                    "ats_type": row["ats_type"],
                    "ats_slug": row["ats_slug"],
                }
                for row in state["companies"]
                if row["careers_url"] not in (None, "none")
            ]
        if table == "jobs" and "url" in params:
            return [row for row in state["jobs"] if row["url"] == params["url"].removeprefix("eq.")]
        return []

    def fake_sb_patch(table, filters, data):
        if table != "companies":
            return True
        for row in state["companies"]:
            if row["id"] == filters["id"]:
                row.update(data)
                return True
        return False

    def fake_sb_insert(table, data):
        if table == "jobs":
            state["jobs"].append(data)
        return True

    class DummyResp:
        text = "<html></html>"

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "sb_patch", fake_sb_patch)
    monkeypatch.setattr(job_scraper, "sb_insert", fake_sb_insert)
    monkeypatch.setattr(job_scraper, "find_careers_url", lambda domain: f"https://{domain}/careers")
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=10: DummyResp())
    monkeypatch.setattr(job_scraper, "detect_ats", lambda html, url: ("generic", None, None))
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: [{
        "title": "VP of Operations",
        "location": "Remote - US",
        "salary_text": "$250,000",
        "url": "https://acme.com/careers/vp-ops",
    }])
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)

    job_scraper.discover_careers("companies")
    job_scraper.scrape_jobs("companies")

    assert state["companies"][0]["careers_url"] == "https://acme.com/careers"
    assert state["companies"][0]["ats_type"] == "generic"
    assert len(state["jobs"]) == 1
    assert state["jobs"][0]["title"] == "VP of Operations"


@pytest.mark.parametrize(
    ("argv", "expected_calls"),
    [
        (["job_scraper.py", "--discover", "--table", "vc"], [("discover", "vc", False)]),
        (["job_scraper.py", "--scrape", "--table", "companies", "--og-only"], [("scrape", "companies", True)]),
    ],
)
def test_main_cli_routes_to_expected_pipeline(monkeypatch, argv, expected_calls):
    calls = []

    monkeypatch.setattr(job_scraper, "SUPABASE_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(job_scraper, "discover_careers", lambda table, og_only=False: calls.append(("discover", table, og_only)))
    monkeypatch.setattr(job_scraper, "scrape_jobs", lambda table, company_id=None, og_only=False: calls.append(("scrape", table, og_only)))

    job_scraper.main()

    assert calls == expected_calls