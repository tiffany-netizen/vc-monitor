import sys

import pytest

import job_scraper
import vc_monitor


@pytest.fixture(autouse=True)
def _stub_stale_close(monkeypatch):
    """Stage-4 stale-close hits Supabase over the network. Stub it out by default so no
    test touches the real DB; tests that assert on it re-patch sb_patch_where themselves."""
    monkeypatch.setattr(job_scraper, "sb_patch_where", lambda table, params, data: 0, raising=False)


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


def test_scrape_jobs_closes_stale_jobs_for_companies(monkeypatch):
    """After a successful scrape, jobs no longer on the board (older last_seen) are closed."""
    fake_companies = [
        {"id": 55, "name": "Acme", "website": "acme.com",
         "careers_url": "https://acme.com/careers", "ats_type": "generic", "ats_slug": None}
    ]
    fake_jobs = [
        {"title": "VP of Operations", "location": "Remote - US",
         "salary_text": "$260,000", "url": "https://acme.com/jobs/vp-ops"}
    ]
    stale_calls = []

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and "select" in params:
            return fake_companies
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url, timeout=8: True)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch_where",
                        lambda table, params, data: stale_calls.append((table, params, data)) or 2)

    job_scraper.scrape_jobs("companies")

    assert len(stale_calls) == 1
    table, params, data = stale_calls[0]
    assert table == "jobs"
    assert params["company_id"] == "eq.55"
    assert params["last_seen"].startswith("lt.")
    assert params["status"] == "in.(new,active)"
    assert data == {"status": "closed"}


def test_scrape_jobs_skips_stale_close_when_scrape_returns_nothing(monkeypatch):
    """A transient fetch failure (0 jobs returned) must NOT close a company's live roles."""
    fake_companies = [
        {"id": 56, "name": "Acme", "website": "acme.com",
         "careers_url": "https://acme.com/careers", "ats_type": "generic", "ats_slug": None}
    ]
    stale_calls = []

    def fake_sb_get(table, params, limit=1000):
        if table == "companies" and "select" in params:
            return fake_companies
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: [])  # scrape returned nothing
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch_where",
                        lambda table, params, data: stale_calls.append((table, params, data)) or 0)

    job_scraper.scrape_jobs("companies")

    assert stale_calls == []


def test_scrape_jobs_vc_stale_close_uses_active_flag(monkeypatch):
    """VC path closes stale vc_jobs via active=false (that table has no status column)."""
    fake_vc_companies = [
        {"id": 91, "company": "Portco", "domain": "portco.co",
         "careers_url": "https://jobs.portco.co", "ats_type": "generic", "ats_slug": None,
         "vc_names": ["Example VC"]}
    ]
    fake_jobs = [
        {"title": "Head of Finance", "location": "Remote - US",
         "salary_text": "$260,000", "url": "https://jobs.portco.co/hof"}
    ]
    stale_calls = []

    def fake_sb_get(table, params, limit=1000):
        if table == "vc_portfolio_companies" and "select" in params:
            return fake_vc_companies
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url, timeout=8: True)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)
    monkeypatch.setattr(job_scraper, "sb_patch_where",
                        lambda table, params, data: stale_calls.append((table, params, data)) or 1)

    job_scraper.scrape_jobs("vc")

    vc_stale = [c for c in stale_calls if c[0] == "vc_jobs"]
    assert len(vc_stale) == 1
    _, params, data = vc_stale[0]
    assert params["company_id"] == "eq.91"
    assert params["active"] == "eq.true"
    assert data == {"active": False}


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

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_greenhouse_null_metadata_does_not_discard_the_board(monkeypatch):
    """Regression: Greenhouse serves optional fields as explicit nulls (e.g.
    "metadata": null). A single such posting used to raise inside the parse loop and
    the board-level except returned [] — silently dropping EVERY role on that board."""
    payload = {
        "jobs": [
            {
                "title": "Chief of Staff",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "offices": [{"name": "New York"}],
                "metadata": None,        # <- the killer
                "location": None,
                "content": "",
            },
            {
                "title": "VP of Finance",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                "offices": None,
                "metadata": [{"name": "Salary", "value": "$250,000 - $300,000"}],
                "location": {"name": "Remote - US"},
                "content": "",
            },
        ]
    }
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=15: _FakeResp(payload))

    jobs = job_scraper.scrape_greenhouse("acme")

    assert [j["title"] for j in jobs] == ["Chief of Staff", "VP of Finance"]
    assert jobs[0]["location"] == "New York"
    assert jobs[1]["location"] == "Remote - US"
    assert jobs[1]["salary_text"] == "$250,000 - $300,000"


def test_lever_null_categories_and_lists_are_survivable(monkeypatch):
    payload = [
        {"text": "Head of Finance", "hostedUrl": "https://jobs.lever.co/acme/1",
         "categories": None, "lists": None, "descriptionPlain": ""},
        {"text": "Controller", "hostedUrl": "https://jobs.lever.co/acme/2",
         "categories": {"allLocations": ["San Francisco"]}, "lists": [], "descriptionPlain": ""},
    ]
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=15: _FakeResp(payload))

    jobs = job_scraper.scrape_lever("acme")

    assert [j["title"] for j in jobs] == ["Head of Finance", "Controller"]
    assert jobs[1]["location"] == "San Francisco"


# ----------------------------------------------------------------
# Regression: Greenhouse embed boards were stored with slug "embed"
# ----------------------------------------------------------------

@pytest.mark.parametrize(
    "html, base_url, expected_slug",
    [
        # The bug: the generic boards.greenhouse.io pattern matched first and
        # captured "embed", so every later API call hit
        # boards-api.greenhouse.io/v1/boards/embed/jobs and 404'd.
        (
            '<a href="https://boards.greenhouse.io/embed/job_board?for=descript">Jobs</a>',
            "https://descript.com/careers",
            "descript",
        ),
        (
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=affinity"></iframe>',
            "https://affinity.co/careers",
            "affinity",
        ),
        # Plain board URLs must keep working unchanged.
        ('<a href="https://boards.greenhouse.io/acme">x</a>', "https://acme.com/careers", "acme"),
        ('<a href="https://job-boards.greenhouse.io/mixmax">x</a>', "https://mixmax.com/careers", "mixmax"),
    ],
)
def test_detect_ats_never_returns_a_reserved_path_as_the_slug(html, base_url, expected_slug):
    ats_type, slug, direct = job_scraper.detect_ats(html, base_url)
    assert ats_type == "greenhouse"
    assert slug == expected_slug
    assert slug not in job_scraper.ATS_RESERVED_SLUGS
    assert direct == f"https://boards.greenhouse.io/{expected_slug}"


# ----------------------------------------------------------------
# Regression: comma-separated and function-first titles were dropped
# ----------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        # ATS platforms write these with a comma; the old regex required
        # whitespace, so all of them were silently discarded.
        "Director, Finance",
        "Director, FP&A",
        "Director, Accounting",
        "Director, Business Operations",
        "Senior Director, Finance",
        "Sr. Director, Finance",
        "VP, Finance",
        "VP, Operations",
        "Vice President, Finance",
        "Vice President, Strategic Finance",   # observed live at PayZen, 2026-07-30
        "Finance Director",                    # function-first word order
        # Forms that already worked must keep working.
        "Director of Finance",
        "VP of Finance",
        "Head of Finance",
        "Chief of Staff",
        "Financial Controller",
    ],
)
def test_title_matches_accepts_comma_and_function_first_titles(title):
    assert job_scraper.title_matches(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # Manager level sits below the target band and must stay excluded even
        # though the comma form now parses.
        "Finance Manager",
        "Senior Manager, Finance",
        "Manager, FP&A",
        # Wrong function entirely.
        "Head of People Operations",
        "Director of Marketing",
        "Sales Director",
        "Director, Product",
        "Software Engineer",
        "Account Executive",
        "Privacy Policy",
    ],
)
def test_title_matches_still_rejects_out_of_band_titles(title):
    assert job_scraper.title_matches(title) is False


# ----------------------------------------------------------------
# Regression: mirrored VC jobs had no company_id, so the UI never showed them
# ----------------------------------------------------------------

def test_resolve_company_id_matches_on_domain_then_name(monkeypatch):
    monkeypatch.setattr(
        job_scraper, "sb_get",
        lambda table, params, limit=1000: [
            {"id": 7, "name": "Acme, Inc.", "website": "https://www.acme.com"},
            {"id": 8, "name": "Ambiguous", "website": ""},
            {"id": 9, "name": "Ambiguous", "website": ""},
        ])
    job_scraper._COMPANY_INDEX = None

    assert job_scraper.resolve_company_id("Acme Inc", "") == 7
    assert job_scraper.resolve_company_id("totally different", "https://acme.com") == 7
    # Two companies share this name, so refuse to guess: a wrong foreign key is
    # worse than a missing one.
    assert job_scraper.resolve_company_id("Ambiguous", "") is None
    assert job_scraper.resolve_company_id("Not Present", "") is None
    job_scraper._COMPANY_INDEX = None


def test_vc_mirror_row_carries_company_id(monkeypatch):
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
    fake_jobs = [{
        "title": "Head of Finance",
        "location": "Remote - US",
        "salary_text": "$260,000",
        "url": "https://jobs.portco.two/hof",
    }]
    inserts = []

    def fake_sb_get(table, params, limit=1000):
        if table == "companies":
            return [{"id": 4242, "name": "Portco Two", "website": "https://portco.two"}]
        if table == "vc_portfolio_companies" and "select" in params:
            return fake_vc_companies
        if table in {"vc_jobs", "jobs"} and "url" in params:
            return []
        return []

    monkeypatch.setattr(job_scraper, "sb_get", fake_sb_get)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", lambda _: fake_jobs)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(job_scraper, "url_is_live", lambda _: True)
    monkeypatch.setattr(job_scraper, "sb_insert", lambda table, data: inserts.append((table, data)) or True)
    monkeypatch.setattr(job_scraper, "sb_patch", lambda table, filters, data: True)
    job_scraper._COMPANY_INDEX = None

    job_scraper.scrape_jobs("vc")

    mirrored = [row for table, row in inserts if table == "jobs"]
    assert len(mirrored) == 1
    # The whole point: without this the Roles UI cannot reach companies.dna_fit.
    assert mirrored[0]["company_id"] == 4242
    job_scraper._COMPANY_INDEX = None
