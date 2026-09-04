# Toggl Focus MCP Server

A Model Context Protocol (MCP) server for Toggl time tracking integration. This server allows Claude and other MCP clients to interact with your Toggl account to manage projects and time entries.

> **Toggl 2.0 (Focus).** Accounts that migrated to Toggl Focus are no longer served by the legacy Toggl Track API — it returns data only up to the migration date, so reports after it come back empty. This server targets the Focus API at `https://focus.toggl.com/api`. See [the Focus API docs](https://engineering.toggl.com/docs/focus/).

> This server is 100% written by Claude Code, except for BUILD_APP.md, which I wrote to guide the creation of the server
> This has not be extensively tested (honestly, I've barely read the code!) so improvements can definitely be made.

![Claude Code](example.png)

## Features

- **Get Projects**: Retrieve all projects from your Toggl workspace
- **Get Workspaces**: Report the organization and workspace the API key resolves to
- **Get Time Entries**: Detailed time entries with filtering by date range and project
- **Time Summary**: Aggregated time reports by project, day and activity
- **Current Timer**: Check what's currently running and elapsed time
- **Timer Control**: Start new timers and stop current running timers
- **Task Management**: Create and retrieve project tasks with time estimates
- **Search Entries**: Find time entries by description text
- **Smart Prompts**: Pre-built conversation starters for common time tracking queries
- Bearer authentication with a key read from the environment
- Organization/workspace discovered from the API rather than hardcoded
- Quota-aware: honours HTTP 402 and the `X-Toggl-Quota-*` headers
- Formatted, readable output for LLM consumption

## Quick Start

### 1. Get Your Toggl Focus API Key

1. Go to [Toggl Focus settings](https://focus.toggl.com/settings)
2. Create an API key (it looks like `toggl_sk_...`)
3. Keep this key handy for the configuration step

> **Only one key is active per user.** Creating a new key revokes the previous one, so update every integration that uses it in the same step.

### 2. Build the Docker Image

Clone this repository and build the Docker image:

```bash
git clone git@github.com:vontell/toggl-track-mcp.git
cd toggl-track-mcp
docker build -t toggl-track-mcp .
```

### 3. Configure Claude Desktop

Add the server to your Claude Desktop configuration file:

**Location:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

```json
{
  "mcpServers": {
    "Toggl": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e",
        "TOGGL_API_KEY",
        "toggl-track-mcp"
      ],
      "env": {
        "TOGGL_API_KEY": "toggl_sk_your_api_key_here"
      }
    }
  }
}
```

**Important:** Replace `"toggl_sk_your_api_key_here"` with your actual Toggl Focus API key. Prefer injecting it from your secret store rather than writing it into a versioned config file.

### 4. Restart Claude Desktop

After updating the configuration, restart Claude Desktop to load the new MCP server.

### 5. Verify Installation

Once restarted, you should be able to ask Claude questions like:
- "What projects do I have in Toggl?"
- "Start a timer for 'Code review'"
- "What's my current timer status?"

---

## Development Setup

For development and testing:

### Local Development

Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set your API token:

```bash
export TOGGL_API_KEY="toggl_sk_your_api_key_here"
```

### Testing with MCP Inspector

```bash
mcp dev server.py
```

This opens the MCP Inspector for interactive testing.

## Available Tools

### `get_projects`

Retrieves all projects from your Toggl workspace with details including:

- Project name and ID
- Active/archived state
- Total tracked time

### `get_workspaces`

Reports the organization and workspace this API key operates on. The Focus API has no
workspace-listing endpoint — a key is scoped to the user's current workspace.

### `get_time_entries`

Get detailed time entries with optional filtering:

- **start_date**: Filter by start date (YYYY-MM-DD format, defaults to 7 days ago)
- **end_date**: Filter by end date (YYYY-MM-DD format, defaults to today)
- **project_name**: Filter by specific project name
- Shows entries grouped by date with descriptions, durations, and daily totals

### `get_time_summary`

Get aggregated time summary by project:

- **start_date**: Start date for summary (defaults to 7 days ago)
- **end_date**: End date for summary (defaults to today)
- **project_name**: Focus on specific project (optional)
- Shows total hours by project with percentages and grand total

### `get_current_timer`

Check currently running timer:

- Shows active project and description
- Displays elapsed time and start time
- Returns "No timer running" if nothing is active

### `start_timer`

Start a new timer:

- **description**: Description for the time entry (required)
- **project_name**: Name of project to assign timer to (optional)
- Automatically uses your primary workspace
- Returns confirmation with timer details

### `stop_current_timer`

Stop the currently running timer:

- Stops any active timer
- Shows final duration and time period
- Returns "No timer running" if nothing is active

### `search_time_entries`

Search time entries by description:

- **query**: Text to search for in descriptions (required)
- **start_date**: Start date for search range (optional, defaults to 30 days ago)
- **end_date**: End date for search range (optional, defaults to today)
- Case-insensitive search with total time calculation

### `get_project_tasks`

Get all tasks for a specific project:

- **project_name**: Name of the project to get tasks for (required)
- Shows task names, IDs, status (active/inactive), and estimated time
- Returns helpful error if project not found

### `create_project_task`

Create a new task for a project:

- **project_name**: Name of the project to create the task in (required)
- **task_name**: Name of the new task (required)
- **estimated_hours**: Estimated hours for the task (optional)
- Returns confirmation with task ID and details

### `get_all_tasks`

Get all tasks across all projects:

- Shows tasks organized by project and workspace
- Displays task names, IDs, status, and estimated time
- Returns total count of tasks found
- Skips projects without task access gracefully

## Example Prompts

The server includes pre-built prompts for common scenarios:

### Time Tracking & Analysis

- **detailed_time_report**: Get detailed breakdown of time entries
- **time_summary_report**: Get aggregated time summary by project
- **productivity_analysis**: Analyze work patterns and productivity
- **current_status_check**: Check current timer and today's activity
- **project_deep_dive**: In-depth analysis of specific project work
- **search_by_description**: Search time entries by description text

### Timer Control

- **quick_start_timer**: Start a timer with description and optional project
- **stop_and_start_new**: Stop current timer and start a new one
- **timer_status_and_control**: Check status and get timer control options
- **work_session_timer**: Start a focused work session with break reminders

### Task Management
- **view_project_tasks**: View all tasks for a specific project
- **create_new_task**: Create a new task with optional time estimates
- **task_planning_session**: Plan and organize tasks for a project
- **project_task_overview**: Get overview of tasks across all projects
- **list_all_tasks**: List all tasks across all projects with details

## Example Usage

Once installed in Claude Desktop, you can ask:

### Project & Workspace Queries

- "What projects do I have in Toggl?"
- "Show me my Toggl workspaces"
- "List all my time tracking projects"

### Time Entry Analysis

- "Show me my time entries for the last week"
- "What did I work on yesterday?"
- "Give me a time summary for project X"
- "How much time did I spend on each project this month?"
- "Search my time entries for 'meeting' this week"

### Timer Control

- "What's my current timer status?"
- "Start a timer for 'Code review' on project ABC"
- "Stop my current timer"
- "Start a timer for 'Planning session'"

### Task Management
- "Show me all tasks for project XYZ"
- "Create a new task called 'Database migration' for project ABC"
- "Create a task with 4 hours estimated time"
- "Help me plan tasks for my current project"
- "List all tasks across all my projects"

## API Reference

This server uses the [Toggl Focus API](https://engineering.toggl.com/docs/focus/), based at
`https://focus.toggl.com/api`. Every data path is scoped by organization and workspace.

| Purpose | Endpoint |
|---|---|
| Discover scope | `GET /users/me/settings` |
| List projects | `GET /organizations/{org}/workspaces/{ws}/projects` |
| List time entries | `GET /organizations/{org}/workspaces/{ws}/time-entries/stream` |
| List tasks | `GET /organizations/{org}/workspaces/{ws}/tasks/stream` |
| Create task | `POST /organizations/{org}/workspaces/{ws}/tasks` |
| Current timer | `GET /organizations/{org}/workspaces/{ws}/tracking/current` |
| Start timer | `POST /organizations/{org}/workspaces/{ws}/tracking/start` |
| Stop timer | `POST /organizations/{org}/workspaces/{ws}/tracking/stop` |

The `/stream` endpoints return a whole date range in one response, which is what keeps a
report inside the hourly quota.

### Scope discovery

The workspace comes from `current_workspace_id` in `GET /users/me/settings`. The Focus API
exposes no endpoint that lists organizations, so the organization is read from the
per-organization maps in that same payload and verified against the API before use. Set
`TOGGL_ORGANIZATION_ID` / `TOGGL_WORKSPACE_ID` to skip discovery.

### Projects, tasks and time entries

A Focus time entry is attributed to a project in one of two ways:

- **Taskless entries** carry `project_id` (and an inlined `project`) directly.
- **Task-bound entries** carry neither; the project comes from the task.

A Focus task need not belong to a project. Time tracked against such a task cannot be
attributed to any project, so the reporting tools list it separately instead of folding it
into a project's total.

## Development

### Project Structure

```
toggl-track-mcp/
├── server.py              # MCP tools and prompts
├── toggl_focus.py         # Toggl Focus API client and helpers
├── tests/                 # Test suite
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
└── README.md              # This file
```

### Running the tests

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest
```

### Adding New Features

To extend this server with additional Toggl functionality:

1. Add new methods to the `TogglFocusClient` class
2. Create new `@mcp.tool()` decorated functions
3. Handle authentication and error cases
4. Update this README with the new capabilities

## Authentication

This server authenticates with `Authorization: Bearer <key>`, reading the key from the
`TOGGL_API_KEY` environment variable (`TOGGL_API_TOKEN` is still accepted for continuity).

**Security Note**: Never commit your API key to version control. Always use environment
variables or secure configuration management. Remember that Toggl allows a single active
key per user — rotating it revokes the old one, so update every consumer at the same time.

## Error Handling

The server includes comprehensive error handling for:

- Missing API key configuration
- Revoked API keys (HTTP 401)
- Exhausted quota (HTTP 402)
- Network connectivity issues
- API authentication failures
- Malformed API responses

## Quota

The Focus API bills a request quota per user per hour (Free: 30 requests/hour; higher on paid
plans). When it runs out the API answers `HTTP 402` and reports `X-Toggl-Quota-Remaining` and
`X-Toggl-Quota-Resets-In`. The server surfaces that as a clear error including the reset time,
caches scope/project/task lookups within a call, and uses the `/stream` endpoints so a date
range costs one request rather than one per page.

## Contributing

Feel free to submit issues and enhancement requests!

## License

This project is open source and available under standard terms.
