#!/usr/bin/env python3
"""
Toggl Focus MCP Server

An MCP server for interacting with the Toggl Focus (Toggl 2.0) API.
Provides tools for time tracking management.

Accounts that migrated to Toggl Focus no longer serve data through the legacy
Toggl Track API, so every tool here talks to https://focus.toggl.com/api.
Reference: https://engineering.toggl.com/docs/focus/
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base

from toggl_focus import (
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
    resolve_date_range,
    task_project_map,
)

# Load environment variables from .env file
load_dotenv()

# Create the MCP server
mcp = FastMCP("Toggl Focus")


def _error(action: str, exc: Exception) -> str:
    """Render an exception as the plain-text answer a tool returns."""
    if isinstance(exc, TogglConfigError):
        return f"Configuration error: {exc}"
    if isinstance(exc, TogglQuotaExceeded):
        return f"Quota error: {exc}"
    return f"Error {action}: {exc}"


async def _find_project(client: TogglFocusClient, project_name: str) -> Dict[str, Any]:
    """Look up a project by name, case-insensitively."""
    projects = await client.get_projects()
    for project in projects:
        if project.get("name", "").lower() == project_name.lower():
            return project
    available = ", ".join(p.get("name", "?") for p in projects) or "none"
    raise TogglAPIError(f"Project '{project_name}' not found. Available projects: {available}")


async def _entries_for_range(
    client: TogglFocusClient,
    start_date: str,
    end_date: str,
    project_name: str = "",
) -> Dict[str, Any]:
    """Fetch and shape the entries backing every reporting tool.

    Returns the entries inside the local-date range, the matched project (if
    any), and the entries that belong to a formal task carrying no project —
    those cannot be attributed to a project and are reported separately rather
    than silently folded into one.
    """
    date_from, date_to = api_window(start_date, end_date)
    project = await _find_project(client, project_name) if project_name else None

    entries = await client.get_time_entries(date_from, date_to)
    entries = [e for e in entries if in_local_range(e, start_date, end_date)]

    task_projects: Dict[int, int] = {}
    if any(is_task_entry(e) for e in entries):
        task_projects = task_project_map(await client.get_tasks())

    unattributed: List[Dict[str, Any]] = []
    if project:
        matched = []
        for entry in entries:
            resolved = entry_project_id(entry, task_projects)
            if resolved == project.get("id"):
                matched.append(entry)
            elif resolved is None and is_task_entry(entry):
                unattributed.append(entry)
        entries = matched

    return {"entries": entries, "project": project, "unattributed": unattributed, "task_projects": task_projects}


def _unattributed_note(unattributed: List[Dict[str, Any]]) -> str:
    """Explain time that belongs to a task with no project, so totals reconcile."""
    if not unattributed:
        return ""
    total = sum(e.get("duration", 0) for e in unattributed)
    labels = sorted({entry_label(e) for e in unattributed})
    return (
        f"\n_Not counted above: {format_duration(total)} tracked against tasks with no project "
        f"({', '.join(labels)}). Assign those tasks to a project for them to appear in a project report._\n"
    )


def _group_by_local_date(entries: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        local = entry_local_datetime(entry)
        grouped[local.strftime("%Y-%m-%d") if local else "Unknown date"].append(entry)
    return grouped


def _project_names(projects: List[Dict[str, Any]]) -> Dict[int, str]:
    return {p.get("id"): p.get("name", "Unknown project") for p in projects}


@mcp.tool()
async def get_workspaces() -> str:
    """
    Get the Toggl Focus organization and workspace this API key operates on.

    Returns the resolved scope. The Focus API has no workspace-listing endpoint:
    a key is scoped to the user's current workspace.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            scope = await client.get_workspace()
            return (
                "**Toggl Focus scope**\n\n"
                f"• Organization ID: {scope['organization_id']}\n"
                f"• Workspace ID: {scope['workspace_id']}\n\n"
                "Discovered from the API (`/users/me/settings`). "
                "Override with TOGGL_ORGANIZATION_ID / TOGGL_WORKSPACE_ID."
            )
    except Exception as exc:
        return _error("fetching workspace", exc)


@mcp.tool()
async def get_projects() -> str:
    """
    Get all projects from Toggl Focus.

    Returns a formatted list of projects with their IDs and tracked time.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            projects = await client.get_projects()
            if not projects:
                return "No projects found in this workspace."

            result = f"**Projects** ({len(projects)}):\n\n"
            for project in projects:
                tracked = project.get("total_tracked_secs", 0)
                result += f"• **{project.get('name', 'Unnamed')}** (ID: {project.get('id')})\n"
                result += f"  - Active: {'Yes' if project.get('active') else 'No'}\n"
                result += f"  - Tracked: {format_duration(tracked)}\n"
                if project.get("archived_at"):
                    result += f"  - Archived: {project['archived_at'][:10]}\n"
                result += "\n"
            return result
    except Exception as exc:
        return _error("fetching projects", exc)


@mcp.tool()
async def get_time_entries(start_date: str = "", end_date: str = "", project_name: str = "") -> str:
    """
    Get time entries from Toggl Focus with optional filtering.

    Args:
        start_date: Start date in YYYY-MM-DD format (optional, defaults to last 7 days)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)
        project_name: Project name to filter by (optional)

    Returns a detailed list of time entries with descriptions and durations.
    Days are bucketed in the timezone each entry was recorded in.
    """
    try:
        start_date, end_date = resolve_date_range(start_date, end_date)
        async with TogglFocusClient(get_api_key()) as client:
            data = await _entries_for_range(client, start_date, end_date, project_name)
            entries = data["entries"]

            if not entries:
                scope = f" for project '{project_name}'" if project_name else ""
                return f"No time entries found from {start_date} to {end_date}{scope}."

            names = _project_names(await client.get_projects())
            result = f"Time Entries ({start_date} to {end_date}):\n\n"

            grouped = _group_by_local_date(entries)
            for date in sorted(grouped):
                day_entries = grouped[date]
                result += f"**{date}**\n"
                daily_total = 0
                for entry in sorted(day_entries, key=lambda e: e.get("start", "")):
                    duration = entry.get("duration", 0)
                    daily_total += max(duration, 0)
                    local = entry_local_datetime(entry)
                    clock = local.strftime("%H:%M") if local else "--:--"
                    project_id = entry_project_id(entry, data["task_projects"])
                    project_label = names.get(project_id, "No project")
                    marker = " †" if is_task_entry(entry) else ""
                    result += (
                        f"  • {clock} | {project_label} | {entry_label(entry)}{marker} | "
                        f"{format_duration(duration) if duration > 0 else 'Running'}\n"
                    )
                result += f"  **Daily Total: {format_duration(daily_total)}**\n\n"

            result += "† = tracked against a formal Toggl Focus task.\n"
            result += _unattributed_note(data["unattributed"])
            return result
    except Exception as exc:
        return _error("fetching time entries", exc)


@mcp.tool()
async def get_time_entries_fixed(start_date: str = "", end_date: str = "", project_name: str = "") -> str:
    """
    Get time entries from Toggl Focus (compatibility alias for get_time_entries).

    Args:
        start_date: Start date in YYYY-MM-DD format (optional, defaults to last 7 days)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)
        project_name: Project name to filter by (optional)

    Kept so existing callers keep working. The single-day workaround this tool
    used to carry is unnecessary against the Focus API, which takes an explicit
    RFC3339 range; both tools now return the same result.
    """
    return await get_time_entries(start_date, end_date, project_name)


@mcp.tool()
async def get_time_summary(start_date: str = "", end_date: str = "", project_name: str = "") -> str:
    """
    Get a summary of tracked time grouped by project, day and activity.

    Args:
        start_date: Start date in YYYY-MM-DD format (optional, defaults to last 7 days)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)
        project_name: Project name to filter by (optional)

    Returns totals per day and per activity. Many Focus entries carry no formal
    task, so activities are grouped by description.
    """
    try:
        start_date, end_date = resolve_date_range(start_date, end_date)
        async with TogglFocusClient(get_api_key()) as client:
            data = await _entries_for_range(client, start_date, end_date, project_name)
            entries = data["entries"]

            if not entries:
                scope = f" for project '{project_name}'" if project_name else ""
                return f"No time entries found from {start_date} to {end_date}{scope}."

            names = _project_names(await client.get_projects())
            total = sum(max(e.get("duration", 0), 0) for e in entries)

            heading = f"'{project_name}'" if project_name else "all projects"
            result = f"**Time Summary — {heading} ({start_date} to {end_date})**\n\n"
            result += f"**Total: {format_duration(total)}** across {len(entries)} entries\n\n"

            if not project_name:
                by_project: Dict[Optional[int], int] = defaultdict(int)
                for entry in entries:
                    by_project[entry_project_id(entry, data["task_projects"])] += max(entry.get("duration", 0), 0)
                result += "**By project**\n\n"
                for project_id, seconds in sorted(by_project.items(), key=lambda kv: kv[1], reverse=True):
                    result += f"• {names.get(project_id, 'No project')}: {format_duration(seconds)}\n"
                result += "\n"

            result += "**By day**\n\n"
            grouped = _group_by_local_date(entries)
            for date in sorted(grouped):
                day_total = sum(max(e.get("duration", 0), 0) for e in grouped[date])
                result += f"• {date}: {format_duration(day_total)}\n"
            result += "\n**By activity**\n\n"

            by_activity: Dict[str, int] = defaultdict(int)
            for entry in entries:
                by_activity[entry_label(entry)] += max(entry.get("duration", 0), 0)
            for label, seconds in sorted(by_activity.items(), key=lambda kv: kv[1], reverse=True):
                result += f"• {label}: {format_duration(seconds)}\n"

            result += _unattributed_note(data["unattributed"])
            return result
    except Exception as exc:
        return _error("generating time summary", exc)


@mcp.tool()
async def search_time_entries(query: str, start_date: str = "", end_date: str = "") -> str:
    """
    Search time entries by description or task name.

    Args:
        query: Text to search for (case-insensitive)
        start_date: Start date in YYYY-MM-DD format (optional, defaults to last 30 days)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)

    Returns matching entries grouped by day.
    """
    try:
        start_date, end_date = resolve_date_range(start_date, end_date, days=30)
        async with TogglFocusClient(get_api_key()) as client:
            data = await _entries_for_range(client, start_date, end_date)
            needle = query.lower()
            matches = [e for e in data["entries"] if needle in entry_label(e).lower()]

            if not matches:
                return f"No time entries matching '{query}' from {start_date} to {end_date}."

            names = _project_names(await client.get_projects())
            total = sum(max(e.get("duration", 0), 0) for e in matches)
            result = f"**Search results for '{query}'** ({start_date} to {end_date})\n\n"
            result += f"Found {len(matches)} entries, {format_duration(total)} total\n\n"

            grouped = _group_by_local_date(matches)
            for date in sorted(grouped):
                result += f"**{date}**\n"
                for entry in sorted(grouped[date], key=lambda e: e.get("start", "")):
                    project_label = names.get(entry_project_id(entry, data["task_projects"]), "No project")
                    result += (
                        f"  • {project_label} | {entry_label(entry)} | "
                        f"{format_duration(entry.get('duration', 0))}\n"
                    )
                result += "\n"
            return result
    except Exception as exc:
        return _error("searching time entries", exc)


@mcp.tool()
async def get_current_timer() -> str:
    """
    Get the currently running Toggl Focus timer.

    Returns details of the running entry, or a note that nothing is tracking.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            current = await client.get_current_tracking()
            if not current:
                return "No timer is currently running."

            names = _project_names(await client.get_projects())
            local = entry_local_datetime(current)
            result = "**Currently running**\n\n"
            result += f"• Activity: {entry_label(current)}\n"
            result += f"• Project: {names.get(entry_project_id(current), 'No project')}\n"
            if local:
                result += f"• Started: {local.strftime('%Y-%m-%d %H:%M')} ({current.get('timezone', 'UTC')})\n"
            result += f"• Entry ID: {current.get('id')}\n"
            return result
    except Exception as exc:
        return _error("fetching current timer", exc)


@mcp.tool()
async def start_timer(description: str, project_name: str = "") -> str:
    """
    Start a new Toggl Focus timer.

    Args:
        description: What you are working on
        project_name: Project to attach the entry to (optional)

    Returns confirmation with the new entry's details.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            project_id = None
            if project_name:
                project_id = (await _find_project(client, project_name)).get("id")

            entry = await client.start_tracking(description=description, project_id=project_id)
            result = "**Timer started**\n\n"
            result += f"• Activity: {description}\n"
            result += f"• Project: {project_name or 'No project'}\n"
            result += f"• Entry ID: {entry.get('id') if entry else 'unknown'}\n"
            return result
    except Exception as exc:
        return _error("starting timer", exc)


@mcp.tool()
async def stop_current_timer() -> str:
    """
    Stop the currently running Toggl Focus timer.

    Returns confirmation with the stopped entry's duration.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            current = await client.get_current_tracking()
            if not current:
                return "No timer is currently running."

            stopped = await client.stop_tracking()
            entry = stopped or current
            result = "**Timer stopped**\n\n"
            result += f"• Activity: {entry_label(entry)}\n"
            result += f"• Duration: {format_duration(entry.get('duration', 0))}\n"
            result += f"• Entry ID: {entry.get('id')}\n"
            return result
    except Exception as exc:
        return _error("stopping timer", exc)


@mcp.tool()
async def get_project_tasks(project_name: str) -> str:
    """
    Get all tasks for a specific project.

    Args:
        project_name: Name of the project

    Returns the project's tasks with status and tracked time.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            project = await _find_project(client, project_name)
            tasks = await client.get_tasks(project_id=project.get("id"))

            if not tasks:
                return f"No tasks found for project '{project.get('name')}'."

            result = f"**Tasks — {project.get('name')}** ({len(tasks)}):\n\n"
            for task in tasks:
                result += f"• **{task.get('name', 'Unnamed')}** (ID: {task.get('id')})\n"
                status = task.get("status")
                if isinstance(status, dict):
                    result += f"  - Status: {status.get('name', 'Unknown')}\n"
                result += f"  - Tracked: {format_duration(task.get('total_tracked_time', 0))}\n"
                if task.get("estimated_mins"):
                    result += f"  - Estimated: {format_duration(task['estimated_mins'] * 60)}\n"
                result += "\n"
            return result
    except Exception as exc:
        return _error("fetching project tasks", exc)


@mcp.tool()
async def get_all_tasks() -> str:
    """
    Get all tasks in the workspace, grouped by project.

    Returns every task with its project, status and tracked time.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            tasks = await client.get_tasks()
            if not tasks:
                return "No tasks found in this workspace."

            names = _project_names(await client.get_projects())
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for task in tasks:
                grouped[names.get(task.get("project_id"), "No project")].append(task)

            result = f"**All tasks** ({len(tasks)}):\n\n"
            for project_label in sorted(grouped):
                result += f"**{project_label}**\n"
                for task in grouped[project_label]:
                    tracked = format_duration(task.get("total_tracked_time", 0))
                    result += f"  • {task.get('name', 'Unnamed')} (ID: {task.get('id')}) — {tracked}\n"
                result += "\n"
            return result
    except Exception as exc:
        return _error("fetching all tasks", exc)


@mcp.tool()
async def create_project_task(project_name: str, task_name: str, estimated_hours: float = 0) -> str:
    """
    Create a new task inside a project.

    Args:
        project_name: Name of the project to create the task in
        task_name: Name of the new task
        estimated_hours: Estimated hours for the task (optional)

    Returns confirmation with the new task's details.
    """
    try:
        async with TogglFocusClient(get_api_key()) as client:
            project = await _find_project(client, project_name)
            estimated_mins = int(estimated_hours * 60) if estimated_hours else None
            task = await client.create_task(
                name=task_name,
                project_id=project.get("id"),
                estimated_mins=estimated_mins,
            )

            result = "**Task created**\n\n"
            result += f"• Task: {task_name}\n"
            result += f"• Project: {project.get('name')}\n"
            if estimated_mins:
                result += f"• Estimated: {format_duration(estimated_mins * 60)}\n"
            result += f"• Task ID: {task.get('id') if task else 'unknown'}\n"
            return result
    except Exception as exc:
        return _error("creating task", exc)


@mcp.prompt()
def start_time_tracking(project_name: str, description: str = "") -> str:
    """Generate a prompt to start time tracking for a project"""
    prompt = f"I want to start tracking time for the project '{project_name}'"
    if description:
        prompt += f" with the description '{description}'"
    prompt += ". Please help me start a new time entry using my Toggl Focus account."
    return prompt


@mcp.prompt()
def weekly_time_report() -> str:
    """Generate a prompt to request a weekly time report"""
    return "Please generate a weekly time report showing my time entries, total hours worked, and project breakdown for this week using my Toggl Focus data."


@mcp.prompt()
def project_time_analysis(project_name: str) -> list[base.Message]:
    """Generate a structured conversation for project time analysis"""
    return [
        base.UserMessage(f"I need to analyze my time tracking for the project '{project_name}'"),
        base.AssistantMessage("I'll help you analyze your time tracking data. Let me first get your projects and recent time entries for this project."),
        base.UserMessage("Please show me the total hours, daily breakdown, and any patterns in my work schedule for this project.")
    ]


@mcp.prompt()
def optimize_workflow() -> str:
    """Generate a prompt for workflow optimization based on time tracking data"""
    return "Based on my Toggl Focus time tracking data, please analyze my work patterns and suggest ways to optimize my workflow and improve productivity."


@mcp.prompt()
def project_overview() -> str:
    """Generate a prompt to get an overview of all projects"""
    return "Please show me all my Toggl Focus projects, organized in a clear format with project details and current status."


@mcp.prompt()
def detailed_time_report(start_date: str, end_date: str = "", project_name: str = "") -> str:
    """Generate a prompt to get detailed time entries for analysis"""
    prompt = f"Please show me detailed time entries from {start_date}"
    if end_date:
        prompt += f" to {end_date}"
    if project_name:
        prompt += f" for the project '{project_name}'"
    prompt += ", including a daily breakdown and totals."
    return prompt


@mcp.prompt()
def project_task_planning(project_name: str) -> str:
    """Generate a prompt for planning tasks for a project"""
    return f"I want to plan out tasks for the project '{project_name}'. Please show me existing tasks and help me create new ones based on the project requirements."


@mcp.prompt()
def project_task_overview() -> str:
    """Generate a prompt for a comprehensive task overview across projects"""
    return "Please give me an overview of tasks across all my projects. Show me which projects have tasks, what needs attention, and help me prioritize my work."


@mcp.prompt()
def list_all_tasks() -> str:
    """Generate a prompt to list all tasks across all projects"""
    return "Please show me all tasks across all my projects, organized by project. Include task status and estimated time for each task."


if __name__ == "__main__":
    # This allows the server to be run directly for testing
    print("Running server...")
    mcp.run()
