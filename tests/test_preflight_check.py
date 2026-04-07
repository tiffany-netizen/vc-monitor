import preflight_check


def test_check_environment_requires_supabase_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    failures = preflight_check.check_environment()

    assert failures == ["SUPABASE_KEY is not set"]


def test_check_table_returns_failure_details_on_http_error(monkeypatch):
    class DummyResponse:
        text = '{"message":"relation does not exist"}'

        def raise_for_status(self):
            raise preflight_check.requests.HTTPError("400 Bad Request")

    monkeypatch.setattr(preflight_check.requests, "get", lambda *args, **kwargs: DummyResponse())

    ok, message = preflight_check.check_table(
        "https://example.supabase.co/rest/v1",
        "test-key",
        "vc_jobs",
        ["id", "title"],
    )

    assert ok is False
    assert "FAIL vc_jobs" in message
    assert "relation does not exist" in message


def test_run_preflight_passes_for_selected_tables(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setattr(preflight_check, "check_table", lambda *args, **kwargs: (True, "OK companies"))

    exit_code = preflight_check.run_preflight(selected_tables=["companies"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "OK companies" in captured.out
    assert "Preflight passed" in captured.out