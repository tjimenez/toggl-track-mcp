"""Tests for the Toggl Focus (Toggl 2.0) client and its entry-shaping helpers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toggl_focus import (  # noqa: E402
    TogglAPIError,
    TogglConfigError,
    TogglFocusClient,
    TogglQuotaExceeded,
    api_window,
    entry_label,
    entry_local_datetime,
    entry_project_id,
    format_duration,
    get_api_key,
    in_local_range,
    is_task_entry,
    organization_candidates,
    resolve_date_range,
    task_project_map,
)

NAME_PROJECT = 1437070


def taskless_entry(start, duration, project_id=NAME_PROJECT, description="Code Review"):
    """A Focus entry with no formal task: carries project_id and description inline."""
    return {
        "id": 1,
        "task_id": None,
        "project_id": project_id,
        "start": start,
        "duration": duration,
        "description": description,
        "timezone": "America/Santo_Domingo",
        "task": None,
        "project": {"id": project_id, "name": "Name"},
    }


def task_entry(start, duration, task_id=14338874, task_name="Planning"):
    """A Focus entry bound to a formal task: carries no project_id at all."""
    return {
        "id": 2,
        "task_id": task_id,
        "project_id": None,
        "start": start,
        "duration": duration,
        "timezone": "America/Santo_Domingo",
        "task": {"id": task_id, "name": task_name},
    }


class FakeResponse:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for aiohttp.ClientSession, replaying queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        return self._responses.pop(0)


def client_with(responses):
    client = TogglFocusClient("toggl_sk_test")
    client.session = FakeSession(responses)
    return client


# -- pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0h 00m"), (1800, "0h 30m"), (2220, "0h 37m"), (28080, "7h 48m"), (67800, "18h 50m")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_duration_treats_running_entries_as_zero():
    assert format_duration(None) == "0h 00m"
    assert format_duration(-1) == "0h 00m"


def test_organization_candidates_reads_per_org_maps():
    settings = {
        "tos_ack_decoupled_licenses": {"178452": "2026-08-23T16:56:04Z"},
        "trial_ended_modal_seen": {"178452": "...", "999": "..."},
    }
    assert organization_candidates(settings) == [178452, 999]


def test_organization_candidates_ignores_non_numeric_and_missing():
    assert organization_candidates({}) == []
    assert organization_candidates({"tos_ack_decoupled_licenses": {"abc": "x"}}) == []


def test_entry_project_id_prefers_inline_project():
    assert entry_project_id(taskless_entry("2026-08-24T13:00:00Z", 600)) == NAME_PROJECT


def test_entry_project_id_falls_back_to_inline_project_object():
    entry = taskless_entry("2026-08-24T13:00:00Z", 600)
    entry["project_id"] = None
    assert entry_project_id(entry) == NAME_PROJECT


def test_entry_project_id_resolves_task_entries_through_the_task_map():
    entry = task_entry("2026-08-24T13:00:00Z", 1800)
    assert entry_project_id(entry) is None
    assert entry_project_id(entry, {14338874: NAME_PROJECT}) == NAME_PROJECT


def test_entry_project_id_is_none_for_a_task_with_no_project():
    """A Focus task need not belong to a project; its time is then unattributed."""
    assert entry_project_id(task_entry("2026-08-24T13:00:00Z", 1800), {}) is None


def test_task_project_map_skips_projectless_tasks():
    tasks = [{"id": 1, "project_id": 10}, {"id": 2}, {"id": 3, "project_id": None}]
    assert task_project_map(tasks) == {1: 10}


def test_entry_label_prefers_task_name_then_description():
    assert entry_label(task_entry("2026-08-24T13:00:00Z", 60)) == "Planning"
    assert entry_label(taskless_entry("2026-08-24T13:00:00Z", 60)) == "Code Review"
    assert entry_label({"description": None, "task": None}) == "(no description)"


def test_is_task_entry():
    assert is_task_entry(task_entry("2026-08-24T13:00:00Z", 60))
    assert not is_task_entry(taskless_entry("2026-08-24T13:00:00Z", 60))


def test_entry_local_datetime_uses_the_entrys_own_timezone():
    """23:30 UTC on the 24th is 19:30 on the 24th in Santo Domingo (UTC-4)."""
    local = entry_local_datetime(taskless_entry("2026-08-24T23:30:00Z", 60))
    assert local.strftime("%Y-%m-%d %H:%M") == "2026-08-24 19:30"


def test_entry_local_datetime_buckets_across_the_utc_day_boundary():
    """02:00 UTC on the 25th is still the 24th locally — the bug UTC bucketing causes."""
    local = entry_local_datetime(taskless_entry("2026-08-25T02:00:00Z", 60))
    assert local.strftime("%Y-%m-%d") == "2026-08-24"


def test_entry_local_datetime_falls_back_to_utc_for_unknown_zones():
    entry = taskless_entry("2026-08-24T23:30:00Z", 60)
    entry["timezone"] = "Not/AZone"
    assert entry_local_datetime(entry).strftime("%Y-%m-%d %H:%M") == "2026-08-24 23:30"


def test_in_local_range_is_inclusive_and_timezone_aware():
    late = taskless_entry("2026-08-29T02:00:00Z", 60)  # 28th, 22:00 local
    early = taskless_entry("2026-08-24T03:00:00Z", 60)  # 23rd, 23:00 local
    assert in_local_range(late, "2026-08-24", "2026-08-28")
    assert not in_local_range(early, "2026-08-24", "2026-08-28")


def test_api_window_pads_the_range_so_local_days_are_complete():
    date_from, date_to = api_window("2026-08-24", "2026-08-28")
    assert date_from == "2026-08-23T00:00:00Z"
    assert date_to == "2026-08-30T00:00:00Z"


def test_api_window_handles_single_day_queries():
    """v1 needed a special 'fixed' tool for this; the padded window covers it."""
    date_from, date_to = api_window("2026-08-24", "2026-08-24")
    assert date_from == "2026-08-23T00:00:00Z"
    assert date_to == "2026-08-26T00:00:00Z"


def test_resolve_date_range_keeps_explicit_dates():
    assert resolve_date_range("2026-08-24", "2026-08-28") == ("2026-08-24", "2026-08-28")


def test_resolve_date_range_backfills_start_from_end():
    assert resolve_date_range("", "2026-08-28")[0] == "2026-08-21"


# -- credentials -----------------------------------------------------------


def test_get_api_key_requires_the_environment(monkeypatch):
    monkeypatch.delenv("TOGGL_API_KEY", raising=False)
    monkeypatch.delenv("TOGGL_API_TOKEN", raising=False)
    with pytest.raises(TogglConfigError, match="TOGGL_API_KEY"):
        get_api_key()


def test_get_api_key_reads_the_secret_from_the_environment(monkeypatch):
    monkeypatch.setenv("TOGGL_API_KEY", "  toggl_sk_from_secret  ")
    assert get_api_key() == "toggl_sk_from_secret"


def test_get_api_key_accepts_the_legacy_variable_name(monkeypatch):
    monkeypatch.delenv("TOGGL_API_KEY", raising=False)
    monkeypatch.setenv("TOGGL_API_TOKEN", "toggl_sk_legacy")
    assert get_api_key() == "toggl_sk_legacy"


# -- transport -------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_raises_on_quota_exhaustion():
    client = client_with([
        FakeResponse(402, headers={"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "1800"})
    ])
    with pytest.raises(TogglQuotaExceeded) as exc:
        await client.request("GET", "/anything")
    assert exc.value.resets_in == 1800
    assert "resets in 1800s" in str(exc.value)


@pytest.mark.asyncio
async def test_request_records_quota_headers_on_success():
    client = client_with([
        FakeResponse(200, payload={}, headers={"X-Toggl-Quota-Remaining": "12", "X-Toggl-Quota-Resets-In": "600"})
    ])
    await client.request("GET", "/anything")
    assert client.quota_remaining == 12
    assert client.quota_resets_in == 600


@pytest.mark.asyncio
async def test_request_returns_none_for_no_content():
    """/tracking/current answers 204 when no timer is running."""
    client = client_with([FakeResponse(204)])
    assert await client.request("GET", "/tracking/current") is None


@pytest.mark.asyncio
async def test_request_reports_a_revoked_key_as_configuration_error():
    client = client_with([FakeResponse(401, text="unauthorized")])
    with pytest.raises(TogglConfigError, match="401"):
        await client.request("GET", "/anything")


@pytest.mark.asyncio
async def test_request_raises_api_error_with_body():
    client = client_with([FakeResponse(500, text="boom")])
    with pytest.raises(TogglAPIError, match="boom"):
        await client.request("GET", "/anything")


@pytest.mark.asyncio
async def test_request_serialises_booleans_and_drops_none_params():
    client = client_with([FakeResponse(200, payload=[])])
    await client.request("GET", "/x", params={"include_taskless": True, "task_id": None, "page": 1})
    assert client.session.calls[0]["params"] == {"include_taskless": "true", "page": 1}


# -- scope discovery -------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_scope_discovers_org_and_workspace_from_the_api(monkeypatch):
    monkeypatch.delenv("TOGGL_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("TOGGL_WORKSPACE_ID", raising=False)
    client = client_with([
        FakeResponse(200, payload={
            "current_workspace_id": 2954942,
            "tos_ack_decoupled_licenses": {"178452": "2026-08-23T16:56:04Z"},
        })
    ])
    assert await client.resolve_scope() == (178452, 2954942)
    assert client.session.calls[0]["url"].endswith("/users/me/settings")


@pytest.mark.asyncio
async def test_resolve_scope_is_cached_to_save_quota(monkeypatch):
    monkeypatch.delenv("TOGGL_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("TOGGL_WORKSPACE_ID", raising=False)
    client = client_with([
        FakeResponse(200, payload={
            "current_workspace_id": 2954942,
            "tos_ack_decoupled_licenses": {"178452": "x"},
        })
    ])
    await client.resolve_scope()
    await client.resolve_scope()
    assert len(client.session.calls) == 1


@pytest.mark.asyncio
async def test_resolve_scope_prefers_explicit_environment_overrides(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "1")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2")
    client = client_with([])
    assert await client.resolve_scope() == (1, 2)
    assert client.session.calls == []


@pytest.mark.asyncio
async def test_resolve_scope_probes_when_several_organizations_are_possible(monkeypatch):
    monkeypatch.delenv("TOGGL_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("TOGGL_WORKSPACE_ID", raising=False)
    client = client_with([
        FakeResponse(200, payload={
            "current_workspace_id": 2954942,
            "tos_ack_decoupled_licenses": {"111": "x", "178452": "y"},
        }),
        FakeResponse(404, text="not found"),   # 111 is not readable
        FakeResponse(200, payload=[]),          # 178452 is
    ])
    assert await client.resolve_scope() == (178452, 2954942)


@pytest.mark.asyncio
async def test_resolve_scope_explains_how_to_recover_when_discovery_fails(monkeypatch):
    monkeypatch.delenv("TOGGL_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("TOGGL_WORKSPACE_ID", raising=False)
    client = client_with([FakeResponse(200, payload={"current_workspace_id": 2954942})])
    with pytest.raises(TogglConfigError, match="TOGGL_ORGANIZATION_ID"):
        await client.resolve_scope()


@pytest.mark.asyncio
async def test_resolve_scope_rejects_non_integer_overrides(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "not-a-number")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2")
    with pytest.raises(TogglConfigError, match="TOGGL_ORGANIZATION_ID"):
        await client_with([]).resolve_scope()


# -- resources -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_projects_follows_pagination(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "178452")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2954942")
    client = client_with([
        FakeResponse(200, payload={"page": 1, "per_page": 2, "total": 3, "data": [{"id": 1}, {"id": 2}]}),
        FakeResponse(200, payload={"page": 2, "per_page": 2, "total": 3, "data": [{"id": 3}]}),
    ])
    projects = await client.get_projects()
    assert [p["id"] for p in projects] == [1, 2, 3]


@pytest.mark.asyncio
async def test_get_projects_is_cached(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "178452")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2954942")
    client = client_with([
        FakeResponse(200, payload={"page": 1, "per_page": 200, "total": 1, "data": [{"id": 1}]}),
    ])
    await client.get_projects()
    await client.get_projects()
    assert len(client.session.calls) == 1


@pytest.mark.asyncio
async def test_get_time_entries_uses_the_stream_endpoint(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "178452")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2954942")
    client = client_with([FakeResponse(200, payload=[taskless_entry("2026-08-24T13:00:00Z", 600)])])
    entries = await client.get_time_entries("2026-08-23T00:00:00Z", "2026-08-30T00:00:00Z")
    call = client.session.calls[0]
    assert call["url"].endswith(
        "/organizations/178452/workspaces/2954942/time-entries/stream"
    )
    assert call["params"]["date_from"] == "2026-08-23T00:00:00Z"
    assert call["params"]["include_taskless"] == "true"
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_get_current_tracking_returns_none_when_idle(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "178452")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2954942")
    client = client_with([FakeResponse(204)])
    assert await client.get_current_tracking() is None


@pytest.mark.asyncio
async def test_start_tracking_posts_to_the_tracking_endpoint(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "178452")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2954942")
    client = client_with([FakeResponse(200, payload={"id": 99})])
    await client.start_tracking(description="Code Review", project_id=NAME_PROJECT)
    call = client.session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/tracking/start")
    assert call["json"]["description"] == "Code Review"
    assert call["json"]["project_id"] == NAME_PROJECT
    assert call["json"]["type"] == "activity"


@pytest.mark.asyncio
async def test_create_task_attaches_the_project(monkeypatch):
    monkeypatch.setenv("TOGGL_ORGANIZATION_ID", "178452")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "2954942")
    client = client_with([FakeResponse(200, payload={"id": 5})])
    await client.create_task(name="Refactor", project_id=NAME_PROJECT, estimated_mins=90)
    call = client.session.calls[0]
    assert call["url"].endswith("/organizations/178452/workspaces/2954942/tasks")
    assert call["json"] == {"name": "Refactor", "project_id": NAME_PROJECT, "estimated_mins": 90}


# -- reporting behaviour ---------------------------------------------------


def test_project_report_separates_time_tracked_against_projectless_tasks():
    """The reason a project total can differ from a naive sum of the range.

    Entries tagged with the project are attributed to it; entries bound to a
    task that has no project cannot be, and must surface separately instead of
    being folded into the project's total.
    """
    entries = [
        taskless_entry("2026-08-24T13:00:00Z", 6300),
        taskless_entry("2026-08-24T16:00:00Z", 4620),
        task_entry("2026-08-24T18:00:00Z", 1800, task_name="Planning"),
        task_entry("2026-08-24T19:00:00Z", 2220, task_id=14338919, task_name="Bugs Fixing"),
    ]
    task_projects = {}  # neither task belongs to a project

    attributed = [e for e in entries if entry_project_id(e, task_projects) == NAME_PROJECT]
    unattributed = [
        e for e in entries if entry_project_id(e, task_projects) is None and is_task_entry(e)
    ]

    assert format_duration(sum(e["duration"] for e in attributed)) == "3h 02m"
    assert format_duration(sum(e["duration"] for e in unattributed)) == "1h 07m"


def test_project_report_includes_task_time_once_the_task_has_a_project():
    entries = [
        taskless_entry("2026-08-24T13:00:00Z", 6300),
        task_entry("2026-08-24T18:00:00Z", 1800, task_name="Planning"),
    ]
    task_projects = {14338874: NAME_PROJECT}
    attributed = [e for e in entries if entry_project_id(e, task_projects) == NAME_PROJECT]
    assert len(attributed) == 2
    assert format_duration(sum(e["duration"] for e in attributed)) == "2h 15m"
