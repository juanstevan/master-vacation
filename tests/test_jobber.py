"""Jobber integration tests against a local fake API. python3 tests/test_jobber.py

Every test here stands for something that breaks a real backfill: a token that
dies at minute 61, a rotated refresh token that was never saved, an unbounded
query, a field the account does not have, a run interrupted at page 400.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.fake_jobber import NOW, FakeJobber, make_jobs, serve  # noqa: E402

from mvh.jobber import client as client_module  # noqa: E402
from mvh.jobber.auth import JobberAuth, Token, TokenStore  # noqa: E402
from mvh.jobber.client import JobberClient, JobberQueryError  # noqa: E402
from mvh.jobber.config import JobberSettings  # noqa: E402
from mvh.jobber.export import (categorize, custom_field_pairs, to_row,  # noqa: E402
                               write_all)
from mvh.jobber.jobs import (build_jobs_query, in_window, load_pages,  # noqa: E402
                             months_ago, parse_time, pull_jobs)

TEMP_DIRS: list[Path] = []


def _tempdir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="mvh-jobber-"))
    TEMP_DIRS.append(path)
    return path


def _connect(state=None, **overrides):
    """A client wired to a fresh fake API, with a token already in hand."""
    state = state or FakeJobber()
    server, base = serve(state)
    workdir = _tempdir()
    settings = JobberSettings(
        client_id="test-id", client_secret="test-secret",
        api_url=f"{base}/api/graphql",
        token_url=f"{base}/api/oauth/token",
        authorize_url=f"{base}/api/oauth/authorize",
        api_version="2025-01-20",
        token_file=workdir / "token.json",
        delay=0.0, page_size=50, timeout=10.0,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    auth = JobberAuth(settings, store=TokenStore(settings.token_file))
    auth.token = Token(access_token=state.access_token,
                       refresh_token=state.refresh_token,
                       expires_at=time.time() + 3600)
    return JobberClient(settings, auth), state, server, workdir


# ------------------------------------------------------------------- helpers
def test_months_ago_lands_on_a_real_date():
    end_of_march = datetime(2026, 3, 31, tzinfo=timezone.utc)
    # One month before 31 March is not 31 February.
    assert months_ago(1, end_of_march).date().isoformat() == "2026-02-28"
    assert months_ago(24, NOW).date().isoformat() == "2024-09-01"


def test_parse_time_handles_the_z_suffix():
    parsed = parse_time("2026-01-15T10:30:00Z")
    assert parsed is not None and parsed.tzinfo is not None
    assert parse_time("") is None and parse_time(None) is None
    assert parse_time("not a date") is None


def test_in_window_falls_back_to_another_date():
    cutoff = months_ago(24, NOW)
    recent = {"createdAt": "2026-08-01T00:00:00Z"}
    old = {"createdAt": "2019-01-01T00:00:00Z"}
    assert in_window(recent, cutoff) and not in_window(old, cutoff)
    # No createdAt: fall back rather than silently dropping the record.
    assert in_window({"startAt": "2026-08-01T00:00:00Z"}, cutoff)
    assert not in_window({"startAt": "2019-01-01T00:00:00Z"}, cutoff)
    assert in_window({}, cutoff), "an undated job should survive to the export"


def test_categorize_reads_the_job_types_the_team_uses():
    assert categorize({"title": "PH ON - Pool Heat"}) == "PH ON"
    assert categorize({"title": "PH OFF"}) == "PH OFF"
    assert categorize({"title": "Pool Heat Off 12/4"}) == "PH OFF"
    assert categorize({"title": "BBQ CLEAN after checkout"}) == "BBQ CLEAN"
    assert categorize({"title": "Guest Report - AC not working"}) == "GUEST REPORT"
    assert categorize({"title": "Quarterly filter change"}) == "OTHER"
    # "OFF" must not be read as "ON".
    assert categorize({"title": "PH OFF"}) != "PH ON"


# ---------------------------------------------------------------- token store
def test_token_file_is_private_and_round_trips():
    path = _tempdir() / "nested" / "token.json"
    store = TokenStore(path)
    store.save(Token(access_token="a", refresh_token="r", expires_at=123.0))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token file is mode {oct(mode)}, must be 0600"
    assert store.load().refresh_token == "r"


def test_env_refresh_token_seeds_then_the_stored_one_takes_over():
    """JOBBER_REFRESH_TOKEN is a starting value, not a permanent one: with
    rotation on it is stale the moment the first refresh happens, so whatever
    got written back has to win."""
    path = _tempdir() / "token.json"
    os.environ["JOBBER_REFRESH_TOKEN"] = "from-env"
    try:
        store = TokenStore(path)
        assert store.load().refresh_token == "from-env", "env should seed"
        store.save(Token(refresh_token="rotated-1"))
        assert path.exists(), "a rotated token must be written down"
        assert store.load().refresh_token == "rotated-1", "stale env var won"
    finally:
        del os.environ["JOBBER_REFRESH_TOKEN"]


def test_refresh_recovers_when_the_stored_token_is_dead_but_env_is_fresh():
    client, state, server, _ = _connect()
    try:
        client.auth.token.refresh_token = "revoked-long-ago"
        client.auth.token.expires_at = 0
        os.environ["JOBBER_REFRESH_TOKEN"] = state.refresh_token
        try:
            assert client.auth.access_token(), "should have recovered via the env var"
        finally:
            del os.environ["JOBBER_REFRESH_TOKEN"]
    finally:
        server.shutdown()


def test_refresh_failure_is_explained_when_there_is_no_fallback():
    client, state, server, _ = _connect()
    try:
        client.auth.token.refresh_token = "revoked-long-ago"
        client.auth.token.expires_at = 0
        try:
            client.auth.access_token()
            raise AssertionError("a revoked refresh token should fail")
        except Exception as exc:
            assert "jobber login" in str(exc), str(exc)
    finally:
        server.shutdown()


def test_rotated_refresh_token_is_saved():
    """Jobber may hand back a new refresh token on every refresh. Losing it
    leaves the next run unable to authenticate at all."""
    client, state, server, _ = _connect()
    try:
        client.auth.token.expires_at = 0          # force a refresh
        client.auth.access_token()
        assert client.auth.token.refresh_token == state.refresh_token
        assert client.auth.token.refresh_token != "refresh-1", "token did not rotate"
        saved = json.loads(client.s.token_file.read_text())
        assert saved["refresh_token"] == state.refresh_token, "rotation not persisted"
    finally:
        server.shutdown()


# ------------------------------------------------------------- query building
def test_query_only_asks_for_fields_the_schema_has():
    client, state, server, _ = _connect()
    try:
        query, dropped = build_jobs_query(client, visits=5)
        # The fake schema has no jobType/endAt/total; the wish list asks anyway.
        assert "jobType" not in query and "endAt" not in query
        assert {"jobType", "endAt", "total"} <= set(dropped), dropped
        # ...and everything that does exist survived.
        for field in ("id", "jobNumber", "title", "jobStatus", "createdAt"):
            assert field in query, f"{field} missing from the query"
        assert "client {" in query and "property {" in query
    finally:
        server.shutdown()


def test_every_connection_is_bounded():
    """An unbounded connection is priced as if 100 nodes came back at every
    level -- the actual reason a 'give me all the jobs' query gets rejected."""
    client, state, server, _ = _connect()
    try:
        query, _ = build_jobs_query(client, visits=5)
        assert "jobs(first: $first" in query
        assert "visits(first: 5)" in query
        assert "assignedUsers(first: 5)" in query
    finally:
        server.shutdown()


def test_custom_fields_union_becomes_inline_fragments():
    client, state, server, _ = _connect()
    try:
        query, _ = build_jobs_query(client, visits=0)
        assert "... on CustomFieldText" in query
        assert "... on CustomFieldNumeric" in query
        assert "__typename" in query
        # valueNumeric belongs only to the numeric member.
        text_block = query.split("... on CustomFieldText")[1].split("}")[0]
        assert "valueText" in text_block and "valueNumeric" not in text_block
    finally:
        server.shutdown()


# ------------------------------------------------------------------ the pull
def test_pull_pages_through_everything_and_applies_the_window():
    client, state, server, workdir = _connect()
    try:
        result = pull_jobs(client, workdir, months=24, page_size=50, now=NOW)
        assert result.complete, "pull should have reached the last page"
        assert result.fetched == 250, result.fetched
        # Jobs are 4 days apart, so a 24-month window keeps roughly 183.
        assert 175 <= result.kept <= 190, result.kept
        assert result.pages == 5, result.pages

        jobs = load_pages(workdir)
        assert len(jobs) == result.kept
        cutoff = months_ago(24, NOW)
        assert all(in_window(job, cutoff) for job in jobs)
        assert jobs[0]["client"]["name"].startswith("Test Client")
        assert jobs[0]["visits"]["nodes"][0]["assignedUsers"]["nodes"][0]["name"]["full"]
    finally:
        server.shutdown()


def test_pull_resumes_instead_of_starting_over():
    client, state, server, workdir = _connect()
    try:
        first = pull_jobs(client, workdir, months=240, page_size=50,
                          max_pages=2, now=NOW)
        assert first.pages == 2, first.pages
        assert not first.complete
        pages_after_first = state.jobs_queries

        second = pull_jobs(client, workdir, months=240, page_size=50, now=NOW)
        assert second.resumed_from_page == 2, "did not resume from the saved cursor"
        assert second.pages == 3, second.pages
        assert second.complete
        # Three more page requests, not five: pages 1-2 were not re-fetched.
        assert state.jobs_queries - pages_after_first == 3, state.jobs_queries

        assert len(load_pages(workdir)) == 250
    finally:
        server.shutdown()


def test_changed_query_starts_a_fresh_pull_rather_than_mixing_shapes():
    client, state, server, workdir = _connect()
    try:
        pull_jobs(client, workdir, months=240, page_size=50, max_pages=1, now=NOW)
        # A different visit count is a different query, so the saved cursor
        # belongs to records of another shape.
        again = pull_jobs(client, workdir, months=240, page_size=50,
                          visits=0, max_pages=1, now=NOW)
        assert again.resumed_from_page == 0, "resumed onto a differently shaped page"
    finally:
        server.shutdown()


# --------------------------------------------------------- failures mid-flight
def test_expired_access_token_is_refreshed_mid_run():
    """The 60-minute token is why backfills die two thirds of the way in."""
    state = FakeJobber()
    state.expire_after_calls = 3          # 401 from the fourth call onward
    client, state, server, workdir = _connect(state)
    try:
        result = pull_jobs(client, workdir, months=240, page_size=50, now=NOW)
        assert result.complete and result.fetched == 250
        assert state.token_calls >= 1, "client never refreshed the token"
    finally:
        server.shutdown()


def test_graphql_throttle_error_is_retried():
    state = FakeJobber()
    state.throttle_on_call = {4, 5}
    client, state, server, workdir = _connect(state)
    try:
        result = pull_jobs(client, workdir, months=240, page_size=50, now=NOW)
        assert result.complete and result.fetched == 250
    finally:
        server.shutdown()


def test_http_429_is_retried():
    state = FakeJobber()
    state.http_429_on_call = {4}
    client, state, server, workdir = _connect(state)
    try:
        result = pull_jobs(client, workdir, months=240, page_size=50, now=NOW)
        assert result.complete and result.fetched == 250
    finally:
        server.shutdown()


def test_client_waits_before_the_budget_runs_out():
    client, state, server, _ = _connect(cost_floor=2000)
    slept: list[float] = []
    real_sleep = client_module.time.sleep
    client_module.time.sleep = lambda s: slept.append(s)
    try:
        client._respect_budget({"currentlyAvailable": 5000, "maximumAvailable": 10000,
                                "restoreRate": 500})
        assert not slept, "no need to wait while well above the floor"
        client._respect_budget({"currentlyAvailable": 500, "maximumAvailable": 10000,
                                "restoreRate": 500})
        assert slept and abs(slept[0] - 3.0) < 0.01, slept
        assert client.last_throttle["currentlyAvailable"] == 500
    finally:
        client_module.time.sleep = real_sleep
        server.shutdown()


def test_missing_version_header_is_explained_not_just_raised():
    client, state, server, _ = _connect(api_version="")
    try:
        client.execute("query Ping { __typename }")
        raise AssertionError("expected the missing version header to fail")
    except JobberQueryError as exc:
        assert "X-JOBBER-GRAPHQL-VERSION" in str(exc)
        assert "JOBBER_API_VERSION" in str(exc), "error should say how to fix it"
    finally:
        server.shutdown()


def test_server_side_filter_support_is_reported():
    """Whether the window can move server-side decides how long a backfill of a
    large account takes, so the schema command has to answer it."""
    client, state, server, _ = _connect()
    try:
        assert "filter" in client.query_args("jobs")
        assert "first" in client.query_args("jobs")
    finally:
        server.shutdown()


# ------------------------------------------------------------------- export
def test_rows_flatten_the_nested_job():
    jobs = make_jobs(3)
    row = to_row(jobs[0])
    assert row["title"] == "PH ON - Pool Heat"
    assert row["category"] == "PH ON"
    assert row["client_name"] == "Test Client 0"
    assert row["address"] == "100 Example Way" and row["city"] == "Testville"
    assert row["assigned_to"] == "Tech 0"
    assert row["visit_count"] == 1
    assert "Problem type: Pool Heat" in row["custom_fields"]
    assert "Priority: 0.0" in row["custom_fields"]


def test_custom_field_union_members_each_yield_a_value():
    pairs = dict(custom_field_pairs(make_jobs(1)[0]))
    assert pairs["Problem type"] == "Pool Heat"
    assert pairs["Priority"] == "0.0"


def test_export_writes_all_three_formats():
    workdir = _tempdir()
    jobs = make_jobs(12)
    written = write_all(jobs, workdir)
    assert {p.name for p in written} == {"jobs.jsonl", "jobs.csv", "jobs.xlsx"}
    for path in written:
        assert path.exists() and path.stat().st_size > 0, path

    import csv
    rows = list(csv.DictReader(open(workdir / "jobs.csv", encoding="utf-8-sig")))
    assert len(rows) == 12
    assert {r["category"] for r in rows} >= {"PH ON", "PH OFF", "BBQ CLEAN",
                                             "GUEST REPORT"}
    # The nested JSON is preserved alongside the flattened view.
    raw = [json.loads(line) for line in
           (workdir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert raw[0]["visits"]["nodes"][0]["assignedUsers"]["nodes"][0]["name"]["full"]


def test_export_survives_jobs_missing_optional_pieces():
    """A job with no property, client, visits or custom fields must still
    produce a row -- one sparse record cannot break the whole export."""
    row = to_row({"id": "j1", "title": "Guest Report - no AC"})
    assert row["job_id"] == "j1" and row["category"] == "GUEST REPORT"
    assert row["client_name"] == "" and row["address"] == ""
    assert row["visit_count"] == 0
    workdir = _tempdir()
    write_all([{"id": "j1", "title": "x"}], workdir)
    assert (workdir / "jobs.xlsx").exists()


# --------------------------------------------------------------- end to end
def test_cli_pull_end_to_end():
    """The real command, start to finish: env credentials in, spreadsheet out."""
    from mvh.cli import main

    state = FakeJobber()
    server, base = serve(state)
    workdir = _tempdir()
    env = {
        "JOBBER_CLIENT_ID": "test-id",
        "JOBBER_CLIENT_SECRET": "test-secret",
        "JOBBER_REFRESH_TOKEN": state.refresh_token,
        "JOBBER_API_URL": f"{base}/api/graphql",
        "JOBBER_TOKEN_URL": f"{base}/api/oauth/token",
        "JOBBER_API_VERSION": "2025-01-20",
        "JOBBER_TOKEN_FILE": str(workdir / "token.json"),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        code = main(["--out", str(workdir), "jobber", "--request-delay", "0",
                     "pull", "--months", "24"])
        assert code == 0, f"pull exited {code}"

        out = workdir / "jobber"
        for name in ("jobs.csv", "jobs.xlsx", "jobs.jsonl", "state.json"):
            assert (out / name).exists(), f"{name} was not written"

        import csv
        rows = list(csv.DictReader(open(out / "jobs.csv", encoding="utf-8-sig")))
        assert 175 <= len(rows) <= 190, f"{len(rows)} rows for a 24-month window"
        assert all(r["job_id"] for r in rows)

        # Re-exporting must not touch the API.
        before = state.graphql_calls
        assert main(["--out", str(workdir), "jobber", "export"]) == 0
        assert state.graphql_calls == before, "export made API calls"

        assert main(["--out", str(workdir), "jobber", "status"]) == 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()


def test_cli_probe_reports_a_healthy_connection():
    from mvh.cli import main

    state = FakeJobber()
    server, base = serve(state)
    env = {
        "JOBBER_CLIENT_ID": "test-id",
        "JOBBER_CLIENT_SECRET": "test-secret",
        "JOBBER_REFRESH_TOKEN": state.refresh_token,
        "JOBBER_API_URL": f"{base}/api/graphql",
        "JOBBER_TOKEN_URL": f"{base}/api/oauth/token",
        "JOBBER_TOKEN_FILE": str(_tempdir() / "token.json"),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        assert main(["jobber", "--request-delay", "0", "probe"]) == 0
        assert main(["jobber", "--request-delay", "0", "schema"]) == 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    for path in TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
