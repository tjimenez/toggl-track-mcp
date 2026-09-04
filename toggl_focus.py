#!/usr/bin/env python3
"""
Toggl Focus (Toggl 2.0) API client.

Toggl accounts migrated from Toggl Track (legacy API v9) to Toggl Focus, which
serves a different API at https://focus.toggl.com/api. This module holds the
transport, scope discovery and entry-shaping helpers used by server.py.

Reference: https://engineering.toggl.com/docs/focus/
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - fallback for very old runtimes
    ZoneInfo = None


TOGGL_FOCUS_API_BASE = os.getenv("TOGGL_API_BASE", "https://focus.toggl.com/api")

# Quota headers documented at https://engineering.toggl.com/docs/focus/
QUOTA_REMAINING_HEADER = "X-Toggl-Quota-Remaining"
QUOTA_RESETS_IN_HEADER = "X-Toggl-Quota-Resets-In"

# Largest page size the API accepts; anything above is rejected as a validation
# error. Kept at the maximum so a listing costs as few quota-billed calls as
# possible.
MAX_PER_PAGE = 100


class TogglConfigError(ValueError):
    """Raised when required configuration (API key, scope) is missing."""


class TogglQuotaExceeded(Exception):
    """Raised when the API answers HTTP 402 because the hourly quota ran out."""

    def __init__(self, remaining: Optional[int] = None, resets_in: Optional[int] = None):
        self.remaining = remaining
        self.resets_in = resets_in
        detail = "Toggl API quota exceeded (HTTP 402)."
        if resets_in is not None:
            detail += f" Quota resets in {resets_in}s."
        detail += (
            " Toggl bills quota per user per hour (Free: 30 requests/hour)."
            " Retry after the reset, or widen the date range so fewer calls are needed."
        )
        super().__init__(detail)


class TogglAPIError(Exception):
    """Raised for any non-successful response that is not a quota error."""


def get_api_key() -> str:
    """Read the Toggl Focus API key from the environment.

    The key is a per-user secret (``toggl_sk_...``) created at
    https://focus.toggl.com/settings. It is never read from source or from
    versioned configuration.
    """
    key = os.getenv("TOGGL_API_KEY") or os.getenv("TOGGL_API_TOKEN")
    if not key:
        raise TogglConfigError(
            "TOGGL_API_KEY environment variable is required. "
            "Create a key at https://focus.toggl.com/settings and provide it as a secret. "
            "Note that Toggl keeps only one active key per user: creating a new one revokes the previous key."
        )
    return key.strip()


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise TogglConfigError(f"{name} must be an integer, got {raw!r}")


class TogglFocusClient:
    """Async client for the Toggl Focus API.

    Every data path is scoped by organization and workspace. Both are discovered
    from the API (see :meth:`resolve_scope`) and cached for the lifetime of the
    client so a single tool call spends as little quota as possible.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None
        self._scope: Optional[Tuple[int, int]] = None
        self._projects: Optional[List[Dict[str, Any]]] = None
        self._tasks: Optional[List[Dict[str, Any]]] = None
        self.quota_remaining: Optional[int] = None
        self.quota_resets_in: Optional[int] = None

    async def __aenter__(self) -> "TogglFocusClient":
        self.session = aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.session:
            await self.session.close()

    def _record_quota(self, headers) -> None:
        for header, attr in (
            (QUOTA_REMAINING_HEADER, "quota_remaining"),
            (QUOTA_RESETS_IN_HEADER, "quota_resets_in"),
        ):
            raw = headers.get(header)
            if raw is None:
                continue
            try:
                setattr(self, attr, int(raw))
            except (TypeError, ValueError):
                pass

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Perform one API call, honouring quota signalling.

        Returns the decoded JSON body, or ``None`` for ``204 No Content``.
        """
        if not self.session:
            raise RuntimeError("Client not initialized. Use async context manager.")

        url = f"{TOGGL_FOCUS_API_BASE}{path}"
        clean_params = None
        if params:
            clean_params = {
                key: ("true" if value is True else "false" if value is False else value)
                for key, value in params.items()
                if value is not None
            }

        async with self.session.request(method, url, params=clean_params, json=json_body) as response:
            self._record_quota(response.headers)

            if response.status == 402:
                raise TogglQuotaExceeded(self.quota_remaining, self.quota_resets_in)
            if response.status == 204:
                return None
            if response.status == 401:
                raise TogglConfigError(
                    "Toggl rejected the API key (HTTP 401). The key may have been revoked — "
                    "Toggl allows a single active key per user, so creating a new one invalidates the old."
                )
            if response.status >= 400:
                body = await response.text()
                raise TogglAPIError(f"{method} {path} failed: {response.status} - {body}")
            return await response.json()

    # -- scope discovery ---------------------------------------------------

    async def resolve_scope(self) -> Tuple[int, int]:
        """Discover the organization and workspace IDs to operate on.

        The workspace comes from ``GET /users/me/settings`` (``current_workspace_id``).
        The Focus API exposes no endpoint that lists organizations, so the
        organization is derived from the per-organization maps carried in that
        same settings payload, and verified against the API before use.

        ``TOGGL_ORGANIZATION_ID`` / ``TOGGL_WORKSPACE_ID`` override discovery.
        """
        if self._scope:
            return self._scope

        org_id = _env_int("TOGGL_ORGANIZATION_ID")
        workspace_id = _env_int("TOGGL_WORKSPACE_ID")

        if org_id and workspace_id:
            self._scope = (org_id, workspace_id)
            return self._scope

        settings = await self.request("GET", "/users/me/settings")

        if not workspace_id:
            workspace_id = settings.get("current_workspace_id")
        if not workspace_id:
            raise TogglConfigError(
                "Could not determine the workspace: /users/me/settings returned no current_workspace_id. "
                "Set TOGGL_WORKSPACE_ID explicitly."
            )

        if not org_id:
            candidates = organization_candidates(settings)
            if not candidates:
                raise TogglConfigError(
                    "Could not discover the organization ID from /users/me/settings. "
                    "Set TOGGL_ORGANIZATION_ID explicitly."
                )
            if len(candidates) == 1:
                org_id = candidates[0]
            else:
                org_id = await self._first_valid_organization(candidates)

        self._scope = (org_id, workspace_id)
        return self._scope

    async def _first_valid_organization(self, candidates: List[int]) -> int:
        """Probe candidate organizations and return the first the key can read."""
        for candidate in candidates:
            try:
                await self.request("GET", f"/organizations/{candidate}/groups/me")
                return candidate
            except TogglAPIError:
                continue
        raise TogglConfigError(
            "None of the discovered organization IDs "
            f"({', '.join(str(c) for c in candidates)}) is readable with this API key. "
            "Set TOGGL_ORGANIZATION_ID explicitly."
        )

    async def _scoped(self, path: str) -> str:
        org_id, workspace_id = await self.resolve_scope()
        return f"/organizations/{org_id}/workspaces/{workspace_id}{path}"

    # -- resources ---------------------------------------------------------

    async def get_workspace(self) -> Dict[str, Any]:
        """Return the resolved workspace scope.

        The Focus API has no workspace-listing endpoint; a key is scoped to the
        user's current workspace, which is what this reports.
        """
        org_id, workspace_id = await self.resolve_scope()
        return {"organization_id": org_id, "workspace_id": workspace_id}

    async def get_projects(self, force: bool = False) -> List[Dict[str, Any]]:
        """List every project in the workspace, following pagination."""
        if self._projects is not None and not force:
            return self._projects

        path = await self._scoped("/projects")
        projects: List[Dict[str, Any]] = []
        page = 1
        while True:
            payload = await self.request("GET", path, params={"page": page, "per_page": MAX_PER_PAGE})
            batch = payload.get("data") or []
            projects.extend(batch)
            total = payload.get("total")
            per_page = payload.get("per_page") or MAX_PER_PAGE
            if not batch or total is None or page * per_page >= total:
                break
            page += 1

        self._projects = projects
        return projects

    async def get_tasks(self, project_id: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
        """List tasks in the workspace, optionally filtered by project."""
        if project_id is None:
            if self._tasks is not None and not force:
                return self._tasks
            path = await self._scoped("/tasks/stream")
            tasks = await self.request("GET", path) or []
            tasks = tasks if isinstance(tasks, list) else tasks.get("data", [])
            self._tasks = tasks
            return tasks

        path = await self._scoped("/tasks/stream")
        tasks = await self.request("GET", path, params={"project_id": project_id}) or []
        return tasks if isinstance(tasks, list) else tasks.get("data", [])

    async def create_task(
        self,
        name: str,
        project_id: Optional[int] = None,
        estimated_mins: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a task, optionally attached to a project."""
        path = await self._scoped("/tasks")
        body: Dict[str, Any] = {"name": name}
        if project_id is not None:
            body["project_id"] = project_id
        if estimated_mins:
            body["estimated_mins"] = estimated_mins
        return await self.request("POST", path, json_body=body)

    async def get_time_entries(
        self,
        date_from: str,
        date_to: str,
        include_taskless: bool = True,
        task_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Stream time entries for an RFC3339 range.

        The ``/stream`` endpoint returns the whole range in a single response,
        which is what keeps a report inside the hourly quota.
        """
        path = await self._scoped("/time-entries/stream")
        payload = await self.request(
            "GET",
            path,
            params={
                "date_from": date_from,
                "date_to": date_to,
                "include_taskless": include_taskless,
                "task_id": task_id,
            },
        )
        if payload is None:
            return []
        return payload if isinstance(payload, list) else payload.get("items", [])

    # -- tracking ----------------------------------------------------------

    async def get_current_tracking(self) -> Optional[Dict[str, Any]]:
        """Return the running time entry, or ``None`` when nothing is tracking."""
        path = await self._scoped("/tracking/current")
        return await self.request("GET", path)

    async def start_tracking(
        self,
        description: str = "",
        project_id: Optional[int] = None,
        task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start tracking a new time entry."""
        path = await self._scoped("/tracking/start")
        body: Dict[str, Any] = {"type": "activity"}
        if description:
            body["description"] = description
        if project_id is not None:
            body["project_id"] = project_id
        if task_id is not None:
            body["task_id"] = task_id
        body["start"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return await self.request("POST", path, json_body=body)

    async def stop_tracking(self) -> Optional[Dict[str, Any]]:
        """Stop the running time entry."""
        path = await self._scoped("/tracking/stop")
        body = {"end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        return await self.request("POST", path, json_body=body)


# -- pure helpers ----------------------------------------------------------


def organization_candidates(settings: Dict[str, Any]) -> List[int]:
    """Extract organization IDs from a ``/users/me/settings`` payload.

    Focus stores several per-organization acknowledgement maps keyed by
    organization ID; those keys are the only organization identifiers the API
    surfaces to a plain user.
    """
    candidates: List[int] = []
    for field in ("tos_ack_decoupled_licenses", "trial_ended_modal_seen"):
        value = settings.get(field)
        if not isinstance(value, dict):
            continue
        for key in value:
            try:
                org_id = int(key)
            except (TypeError, ValueError):
                continue
            if org_id not in candidates:
                candidates.append(org_id)
    return candidates


def format_duration(seconds: int) -> str:
    """Render a duration in seconds as ``Xh YYm``."""
    seconds = max(int(seconds or 0), 0)
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def entry_timezone(entry: Dict[str, Any], default: str = "UTC"):
    """Return the tzinfo an entry was recorded in.

    Focus records ``start`` in UTC plus the ``timezone`` the user was in, so a
    day breakdown only lines up with what the user saw if it buckets by that
    zone rather than by UTC.
    """
    name = entry.get("timezone") or default
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def entry_local_datetime(entry: Dict[str, Any], default_tz: str = "UTC") -> Optional[datetime]:
    """Return the entry start time in the entry's own timezone."""
    start = entry.get("start")
    if not start:
        return None
    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return parsed.astimezone(entry_timezone(entry, default_tz))


def entry_project_id(entry: Dict[str, Any], task_projects: Optional[Dict[int, int]] = None) -> Optional[int]:
    """Resolve the project an entry belongs to.

    Taskless entries carry ``project_id`` (and an inlined ``project``) directly.
    Task-bound entries carry neither, so the project has to come from the task —
    and a task without a project leaves the entry genuinely unattributed.
    """
    project_id = entry.get("project_id")
    if project_id:
        return project_id
    inline = entry.get("project")
    if isinstance(inline, dict) and inline.get("id"):
        return inline["id"]
    task_id = entry.get("task_id")
    if task_id and task_projects:
        return task_projects.get(task_id)
    return None


def entry_label(entry: Dict[str, Any]) -> str:
    """Human label for an entry: its task name, else its free-text description."""
    task = entry.get("task")
    if isinstance(task, dict) and task.get("name"):
        return task["name"]
    return entry.get("description") or "(no description)"


def is_task_entry(entry: Dict[str, Any]) -> bool:
    """True when the entry is attached to a formal Focus task."""
    return bool(entry.get("task_id"))


def task_project_map(tasks: List[Dict[str, Any]]) -> Dict[int, int]:
    """Build ``task_id -> project_id`` for tasks that belong to a project."""
    mapping: Dict[int, int] = {}
    for task in tasks:
        task_id = task.get("id")
        project_id = task.get("project_id")
        if task_id and project_id:
            mapping[task_id] = project_id
    return mapping


def resolve_date_range(start_date: str, end_date: str, days: int = 7) -> Tuple[str, str]:
    """Normalise the tool-level ``YYYY-MM-DD`` inputs, defaulting to the last week."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not start_date and not end_date:
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"), today
    if not start_date:
        start = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days)
        return start.strftime("%Y-%m-%d"), end_date
    if not end_date:
        return start_date, today
    return start_date, end_date


def api_window(start_date: str, end_date: str) -> Tuple[str, str]:
    """Widen a local date range into the RFC3339 window to ask the API for.

    A local day can start up to a day away from its UTC boundary, so the query
    is padded and the results are then bucketed by each entry's own timezone.
    This is why v1's ``get_time_entries_fixed`` single-day workaround is no
    longer needed.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=1)
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=2)
    return start.strftime("%Y-%m-%dT00:00:00Z"), end.strftime("%Y-%m-%dT00:00:00Z")


def in_local_range(entry: Dict[str, Any], start_date: str, end_date: str, default_tz: str = "UTC") -> bool:
    """True when the entry falls inside the inclusive local-date range."""
    local = entry_local_datetime(entry, default_tz)
    if local is None:
        return False
    day = local.strftime("%Y-%m-%d")
    return start_date <= day <= end_date
