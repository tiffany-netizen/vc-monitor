"""Per-company cap and blank-frame skip.

A child frame that never commits (about:blank, hidden, detached) makes
Playwright's frame.content() wait forever. That used to freeze careers
discovery on the first such company. The cap bounds discover and scrape;
a discovery miss, failure, or timeout still writes careers_url='none'.
"""
import threading
import time

import pytest

import job_scraper

NONE = {"careers_url": "none", "ats_type": None, "ats_slug": None}


class _Frame:
    def __init__(
        self,
        url,
        parent=object(),
        detached=False,
        visible=True,
        blows_up=False,
        element_error=False,
    ):
        self.url = url
        self.parent_frame = parent
        self._detached = detached
        self._visible = visible
        self._blows_up = blows_up
        self._element_error = element_error
        self.content_called = False

    def is_detached(self):
        return self._detached

    def frame_element(self):
        if self._element_error:
            raise RuntimeError("no element")
        return self

    def is_visible(self):
        return self._visible

    def content(self):
        self.content_called = True
        if self._blows_up:
            raise RuntimeError("frame content failed")
        return f"<html>{self.url}</html>"


def test_blank_hidden_and_detached_frames_are_not_read():
    # justswish.in: a YouTube iframe that never leaves about:blank, plus a
    # hidden embed. The main document and a visible Greenhouse board are read.
    main = _Frame("https://justswish.in/", parent=None)
    blank = _Frame("about:blank")
    srcdoc = _Frame("about:srcdoc")
    empty = _Frame("")
    hidden = _Frame("https://www.youtube.com/embed/abc", visible=False)
    detached = _Frame("https://example.com/orphan", detached=True)
    board = _Frame("https://boards.greenhouse.io/embed/job_board?for=acme")

    htmls = job_scraper._html_from_frames(
        [main, blank, srcdoc, empty, hidden, detached, board], main
    )

    assert htmls == [
        "<html>https://justswish.in/</html>",
        "<html>https://boards.greenhouse.io/embed/job_board?for=acme</html>",
    ]
    assert main.content_called and board.content_called
    for skipped in (blank, srcdoc, empty, hidden, detached):
        assert not skipped.content_called


def test_main_frame_is_read_even_when_its_url_is_blank():
    main = _Frame("about:blank", parent=None)
    htmls = job_scraper._html_from_frames([main], main)
    assert htmls == ["<html>about:blank</html>"]
    assert main.content_called


def test_frame_content_error_does_not_drop_later_frames():
    main = _Frame("https://example.com/", parent=None)
    bad = _Frame("https://boards.greenhouse.io/bad", blows_up=True)
    good = _Frame("https://boards.greenhouse.io/good")
    htmls = job_scraper._html_from_frames([main, bad, good], main)
    assert htmls == [
        "<html>https://example.com/</html>",
        "<html>https://boards.greenhouse.io/good</html>",
    ]
    assert bad.content_called


def test_visibility_check_failure_still_reads_embedded_board():
    main = _Frame("https://example.com/", parent=None)
    board = _Frame("https://jobs.ashbyhq.com/acme", element_error=True)
    htmls = job_scraper._html_from_frames([main, board], main)
    assert board.content_called
    assert len(htmls) == 2


def test_company_cap_returns_value_and_reraises():
    assert job_scraper.run_with_company_cap(lambda: 41, timeout=2) == 41

    def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        job_scraper.run_with_company_cap(boom, timeout=2)


def test_company_cap_does_not_wait_out_a_hung_call():
    released = threading.Event()

    def hang():
        released.wait(5)
        return "late"

    started = time.monotonic()
    try:
        with pytest.raises(job_scraper.CompanyTimeCapExceeded):
            job_scraper.run_with_company_cap(hang, timeout=0.3)
    finally:
        released.set()
    assert time.monotonic() - started < 3


def _vc_rows(*rows):
    def fake_sb_get(table, params, limit=1000):
        if table in ("vc_jobs", "jobs", "companies"):
            return []
        return list(rows)
    return fake_sb_get


def test_discovery_timeout_records_none_and_continues(monkeypatch):
    monkeypatch.setattr(job_scraper, "COMPANY_TIME_CAP_SECONDS", 0.4)
    monkeypatch.setattr(job_scraper, "_CAP_GRACE_SECONDS", 0)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda *_a, **_k: None)
    released = threading.Event()
    seen = []

    def find(domain):
        seen.append(domain)
        if domain == "hung.example":
            released.wait(5)
            return None
        return "https://ok.example/careers"

    class _Resp:
        text = "<html></html>"

    patches = []
    monkeypatch.setattr(job_scraper, "find_careers_url", find)
    monkeypatch.setattr(job_scraper, "safe_get", lambda url, timeout=10: _Resp())
    monkeypatch.setattr(
        job_scraper, "detect_ats",
        lambda html, url: ("greenhouse", "ok", "https://boards.greenhouse.io/ok"),
    )
    monkeypatch.setattr(job_scraper, "sb_get", _vc_rows(
        {"id": 1, "company": "Hung", "domain": "hung.example"},
        {"id": 2, "company": "Ok", "domain": "ok.example"},
    ))
    monkeypatch.setattr(
        job_scraper, "sb_patch",
        lambda t, f, d: patches.append((f["id"], d)) or True,
    )

    started = time.monotonic()
    try:
        job_scraper.discover_careers("vc")
    finally:
        released.set()
        time.sleep(0.05)

    assert time.monotonic() - started < 3
    assert seen[0] == "hung.example"
    assert "ok.example" in seen
    assert patches[0] == (1, NONE)
    assert patches[1] == (2, {
        "careers_url": "https://boards.greenhouse.io/ok",
        "ats_type": "greenhouse",
        "ats_slug": "ok",
    })


def test_discovery_exception_records_none(monkeypatch):
    monkeypatch.setattr(job_scraper.time, "sleep", lambda *_a, **_k: None)

    def boom(domain):
        raise RuntimeError("render crashed")

    patches = []
    monkeypatch.setattr(job_scraper, "find_careers_url", boom)
    monkeypatch.setattr(job_scraper, "sb_get", _vc_rows(
        {"id": 9, "company": "Boom", "domain": "boom.example"},
    ))
    monkeypatch.setattr(
        job_scraper, "sb_patch",
        lambda t, f, d: patches.append(d) or True,
    )

    job_scraper.discover_careers("vc")
    assert patches == [NONE]


def test_discovery_miss_records_none_and_clears_ats(monkeypatch):
    monkeypatch.setattr(job_scraper.time, "sleep", lambda *_a, **_k: None)
    patches = []
    monkeypatch.setattr(job_scraper, "find_careers_url", lambda domain: None)
    monkeypatch.setattr(job_scraper, "sb_get", _vc_rows(
        {"id": 4, "company": "No Careers", "domain": "nocareers.example"},
    ))
    monkeypatch.setattr(
        job_scraper, "sb_patch",
        lambda t, f, d: patches.append(d) or True,
    )

    job_scraper.discover_careers("vc")
    assert patches == [NONE]


def test_scrape_timeout_skips_company_without_clearing_careers_url(monkeypatch):
    monkeypatch.setattr(job_scraper, "COMPANY_TIME_CAP_SECONDS", 0.4)
    monkeypatch.setattr(job_scraper, "_CAP_GRACE_SECONDS", 0)
    monkeypatch.setattr(job_scraper.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(job_scraper, "url_is_live", lambda url, timeout=8: True)
    monkeypatch.setattr(job_scraper, "resolve_company_id", lambda name, domain="": None)
    monkeypatch.setattr(job_scraper, "sb_patch_where", lambda table, params, data: 0)
    released = threading.Event()
    calls = []

    def get_jobs(co):
        calls.append(co["id"])
        if co["id"] == 1:
            released.wait(5)
            return []
        return [{
            "title": "VP of Operations",
            "location": "Remote - US",
            "salary_text": "$250,000",
            "url": "https://ok.example/jobs/vp",
        }]

    rows = [
        {
            "id": 1, "company": "Hung", "domain": "hung.example",
            "careers_url": "https://hung.example/careers",
            "ats_type": None, "ats_slug": None, "vc_names": "",
        },
        {
            "id": 2, "company": "Ok", "domain": "ok.example",
            "careers_url": "https://ok.example/careers",
            "ats_type": None, "ats_slug": None, "vc_names": "",
        },
    ]
    patches = []
    inserts = []
    monkeypatch.setattr(job_scraper, "sb_get", _vc_rows(*rows))
    monkeypatch.setattr(
        job_scraper, "sb_patch",
        lambda t, f, d: patches.append((t, d)) or True,
    )
    monkeypatch.setattr(job_scraper, "sb_insert", lambda t, d: inserts.append((t, d)) or True)
    monkeypatch.setattr(job_scraper, "get_jobs_for_company", get_jobs)

    started = time.monotonic()
    try:
        job_scraper.scrape_jobs("vc")
    finally:
        released.set()
        time.sleep(0.05)

    assert time.monotonic() - started < 3
    assert calls[0] == 1
    assert 2 in calls
    assert all(data.get("careers_url") != "none" for _table, data in patches)
    assert any(table == "vc_portfolio_companies" and "last_scraped" in data for table, data in patches)
    assert any(table == "vc_jobs" for table, _data in inserts)
