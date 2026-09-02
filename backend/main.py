"""
Issue Tracker — a simple internal tool for tracking shipment issues.

This file is the ENTIRE app: the web page AND the API, in one file.
Substrait builds this file into a container and serves it on port 8000.

How it works:
  - When deployed, a database (OceanBase / MySQL) stores issues permanently.
  - When running locally without a database, issues are stored in memory.
    Everything works, but data is lost on restart.
  - The web page at "/" talks to the API at "/api/..." using fetch().

Key rules this file follows (set by Substrait):
  1. Server listens on port 8000        (configured in cicd/Dockerfile.backend)
  2. GET /health returns HTTP 200        (Substrait's readiness check)
  3. All JSON endpoints start with /api  (Substrait routes /api to here)
"""

import os
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# aiomysql is the database driver for MySQL / OceanBase.
# We import it conditionally so the app still starts locally without it.
try:
    import aiomysql
except ImportError:
    aiomysql = None

# ---------------------------------------------------------------------------
# Logging — so we can see what the app is doing
# ---------------------------------------------------------------------------
# Logs go to stdout (the console). Locally, you see them in the terminal.
# Deployed, logs are not visible — so errors are also returned as readable
# messages in the API response, which the page shows to the user.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("issue_tracker")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

APP_NAME = "Issue Tracker"
app = FastAPI(title=APP_NAME, docs_url="/api/docs")

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------
# db_pool holds the connection pool when a database is available.
# When it's None, the app uses in-memory storage instead.

db_pool: Any = None

# In-memory storage for local mode (no DATABASE_URL).
# This is just a list of dictionaries — each dict is one issue.
memory_issues: list[dict] = []
memory_next_id: int = 1  # simulates the database's auto-increment


def is_database_mode() -> bool:
    """Return True if a real database is connected, False for in-memory mode."""
    return db_pool is not None


def parse_database_url(url: str) -> dict:
    """
    Parse a DATABASE_URL into connection parameters.

    The URL looks like:  mysql://username:password@host:3306/dbname

    Username and password may be percent-encoded (e.g. %40 for @),
    so we decode them with unquote(). A hand-rolled split would fail
    at runtime with "Access denied" — always use a real parser.
    """
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": (parsed.path or "/").lstrip("/"),
    }


# ---------------------------------------------------------------------------
# Data access functions
# ---------------------------------------------------------------------------
# These functions work with BOTH database and in-memory modes.
# The API routes below call these — they don't need to know which mode
# is active. This makes the code easy to test and maintain.


async def list_issues() -> list[dict]:
    """Return all issues, newest first."""
    if db_pool:
        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id, reference, lane, issue_type, status, "
                    "owner, notes, created_at FROM issues "
                    "ORDER BY id DESC"
                )
                rows = await cur.fetchall()
                # Convert datetime objects to strings for JSON
                for row in rows:
                    if row.get("created_at"):
                        row["created_at"] = row["created_at"].strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                return rows
    else:
        # Local mode: return a copy, sorted newest first
        return sorted(
            [dict(i) for i in memory_issues],
            key=lambda x: x["id"],
            reverse=True,
        )


async def create_issue(
    reference: str,
    lane: str,
    issue_type: str,
    status: str,
    owner: str,
    notes: str,
) -> dict:
    """Insert a new issue and return it as a dict."""
    if db_pool:
        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "INSERT INTO issues (reference, lane, issue_type, "
                    "status, owner, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (reference, lane, issue_type, status, owner, notes),
                )
                await conn.commit()
                issue_id = cur.lastrowid
                return {
                    "id": issue_id,
                    "reference": reference,
                    "lane": lane,
                    "issue_type": issue_type,
                    "status": status,
                    "owner": owner,
                    "notes": notes,
                    "created_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
    else:
        global memory_next_id
        issue = {
            "id": memory_next_id,
            "reference": reference,
            "lane": lane,
            "issue_type": issue_type,
            "status": status,
            "owner": owner,
            "notes": notes,
            "created_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        memory_issues.append(issue)
        memory_next_id += 1
        return dict(issue)


async def update_issue_status(issue_id: int, status: str) -> dict | None:
    """
    Update the status of an issue.
    Returns the updated issue, or None if the issue was not found.
    """
    if db_pool:
        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE issues SET status = %s WHERE id = %s",
                    (status, issue_id),
                )
                await conn.commit()
                if cur.rowcount == 0:
                    return None
                await cur.execute(
                    "SELECT id, reference, lane, issue_type, status, "
                    "owner, notes, created_at FROM issues WHERE id = %s",
                    (issue_id,),
                )
                row = await cur.fetchone()
                if row and row.get("created_at"):
                    row["created_at"] = row["created_at"].strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                return row
    else:
        for issue in memory_issues:
            if issue["id"] == issue_id:
                issue["status"] = status
                return dict(issue)
        return None


async def delete_issue(issue_id: int) -> bool:
    """Delete an issue. Returns True if deleted, False if not found."""
    if db_pool:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM issues WHERE id = %s", (issue_id,)
                )
                await conn.commit()
                return cur.rowcount > 0
    else:
        for i, issue in enumerate(memory_issues):
            if issue["id"] == issue_id:
                memory_issues.pop(i)
                return True
        return False


# ---------------------------------------------------------------------------
# Pydantic models — define what the API expects in request bodies
# ---------------------------------------------------------------------------
# FastAPI uses these to validate input automatically and generate docs.


class IssueCreate(BaseModel):
    """Fields needed to create a new issue."""
    reference: str
    lane: str
    issue_type: str
    status: str = "open"  # defaults to "open" if not provided
    owner: str
    notes: str = ""


class StatusUpdate(BaseModel):
    """Fields needed to update an issue's status."""
    status: str  # "open" or "closed"


# ---------------------------------------------------------------------------
# API routes — all JSON endpoints start with /api
# ---------------------------------------------------------------------------


@app.get("/health", tags=["system"])
def health():
    """Substrait calls this to check if the app started correctly."""
    return {"status": "ok"}


@app.get("/api/info", tags=["system"])
def info():
    """Return app info and current storage mode (database vs in-memory)."""
    return {
        "app": APP_NAME,
        "mode": "database" if is_database_mode() else "local (in-memory)",
        "server_time": datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
    }


@app.get("/api/issues", tags=["issues"])
async def get_issues():
    """Return all issues as a JSON list, newest first."""
    try:
        issues = await list_issues()
        mode = "db" if is_database_mode() else "memory"
        log.info(f"Listed {len(issues)} issues (mode: {mode})")
        return issues
    except Exception as e:
        log.error(f"Failed to list issues: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Could not load issues: {e}"},
        )


@app.post("/api/issues", tags=["issues"], status_code=201)
async def create_new_issue(issue: IssueCreate):
    """Create a new issue."""
    try:
        # Only "open" or "closed" are valid statuses
        if issue.status not in ("open", "closed"):
            issue.status = "open"
        created = await create_issue(
            issue.reference,
            issue.lane,
            issue.issue_type,
            issue.status,
            issue.owner,
            issue.notes,
        )
        log.info(
            f"Created issue {created['id']}: {issue.reference} "
            f"({issue.lane}) — {issue.issue_type}"
        )
        return created
    except Exception as e:
        log.error(f"Failed to create issue: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Could not create issue: {e}"},
        )


@app.patch("/api/issues/{issue_id}/status", tags=["issues"])
async def update_status(issue_id: int, body: StatusUpdate):
    """Update the status of an issue (open to closed, or closed to open)."""
    try:
        if body.status not in ("open", "closed"):
            return JSONResponse(
                status_code=400,
                content={"error": "Status must be 'open' or 'closed'"},
            )
        updated = await update_issue_status(issue_id, body.status)
        if updated is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Issue {issue_id} not found"},
            )
        log.info(f"Updated issue {issue_id} status to '{body.status}'")
        return updated
    except Exception as e:
        log.error(f"Failed to update issue {issue_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Could not update issue: {e}"},
        )


@app.delete("/api/issues/{issue_id}", tags=["issues"])
async def delete_issue_route(issue_id: int):
    """Delete an issue by its ID."""
    try:
        deleted = await delete_issue(issue_id)
        if not deleted:
            return JSONResponse(
                status_code=404,
                content={"error": f"Issue {issue_id} not found"},
            )
        log.info(f"Deleted issue {issue_id}")
        return {"deleted": True, "id": issue_id}
    except Exception as e:
        log.error(f"Failed to delete issue {issue_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Could not delete issue: {e}"},
        )


# ---------------------------------------------------------------------------
# Startup and shutdown — connect to database on start, close on stop
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup():
    """
    Connect to the database if DATABASE_URL is set.
    Otherwise, fall back to in-memory storage.
    """
    global db_pool
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if aiomysql is None:
            log.error(
                "DATABASE_URL is set but aiomysql is not installed. "
                "Falling back to in-memory storage."
            )
            return
        try:
            params = parse_database_url(db_url)
            log.info(
                f"Connecting to database at "
                f"{params['host']}:{params['port']}/{params['db']}"
            )
            db_pool = await aiomysql.create_pool(
                host=params["host"],
                port=params["port"],
                user=params["user"],
                password=params["password"],
                db=params["db"],
                charset="utf8mb4",
                autocommit=False,
                minsize=2,
                maxsize=10,
            )
            log.info("Database connected — issues will be saved permanently.")
        except Exception as e:
            log.error(f"Could not connect to database: {e}")
            log.warning("Falling back to in-memory storage.")
            db_pool = None
    else:
        log.info("No DATABASE_URL found — running in local mode (in-memory).")
        log.info("Issues will be lost when the app restarts.")


@app.on_event("shutdown")
async def shutdown():
    """Close the database connection pool on shutdown."""
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        log.info("Database connection closed.")


# ---------------------------------------------------------------------------
# Web page — served at "/"
# ---------------------------------------------------------------------------
# This HTML is the full UI. It uses JavaScript to call the API and render
# the issues table, summary bar, and form.

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Issue Tracker</title>
<style>
  /* ---- Design tokens: colors, fonts, spacing used throughout ---- */
  :root {
    --bg: #f1f5f9;
    --card: #ffffff;
    --text: #0f172a;
    --text-secondary: #64748b;
    --border: #e2e8f0;
    --primary: #4f46e5;
    --primary-hover: #4338ca;
    --open-bg: #fef3c7;
    --open-text: #92400e;
    --open-dot: #f59e0b;
    --closed-bg: #d1fae5;
    --closed-text: #065f46;
    --closed-dot: #10b981;
    --danger: #dc2626;
    --danger-hover: #b91c1c;
    --radius: 14px;
    --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 32px 24px;
    min-height: 100vh;
  }

  .container { max-width: 1100px; margin: 0 auto; }

  /* ---- Header ---- */
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 28px;
  }
  .header h1 {
    font-size: 28px;
    font-weight: 700;
    letter-spacing: -.02em;
  }
  .header p {
    color: var(--text-secondary);
    font-size: 15px;
    margin-top: 4px;
  }
  .mode-badge {
    padding: 6px 14px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    white-space: nowrap;
  }
  .mode-badge.local  { background: var(--open-bg);  color: var(--open-text); }
  .mode-badge.live   { background: var(--closed-bg); color: var(--closed-text); }
  .mode-badge.checking { background: #e2e8f0; color: var(--text-secondary); }

  /* ---- Local mode notice ---- */
  .notice {
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 14px;
    margin-bottom: 24px;
  }
  .notice.local {
    background: #fef3c7;
    color: #92400e;
    border: 1px solid #fde68a;
  }

  /* ---- Summary bar ---- */
  .summary {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 24px;
    box-shadow: var(--shadow);
  }
  .stat-card .label {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-secondary);
  }
  .stat-card .value {
    font-size: 36px;
    font-weight: 700;
    margin-top: 4px;
  }
  .stat-card.open   .value { color: var(--open-dot); }
  .stat-card.closed .value { color: var(--closed-dot); }
  .stat-card.total  .value { color: var(--text); }

  /* ---- Cards ---- */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 24px;
    overflow: hidden;
  }
  .card-header {
    padding: 18px 24px;
    border-bottom: 1px solid var(--border);
    font-size: 16px;
    font-weight: 600;
  }
  .card-body { padding: 24px; }

  /* ---- Form ---- */
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 16px;
    align-items: end;
  }
  .form-group { display: flex; flex-direction: column; }
  .form-group label {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }
  .form-group input,
  .form-group select {
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    transition: border-color .15s, box-shadow .15s;
  }
  .form-group input:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(79,70,229,.1);
  }
  .form-actions {
    margin-top: 16px;
    display: flex;
    justify-content: flex-end;
  }

  /* ---- Buttons ---- */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 10px 20px;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: background .15s, transform .05s;
  }
  .btn:active { transform: scale(.98); }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-sm { padding: 6px 12px; font-size: 13px; border-radius: 6px; }
  .btn-toggle { background: var(--open-bg); color: var(--open-text); }
  .btn-toggle.closed { background: var(--closed-bg); color: var(--closed-text); }
  .btn-delete {
    background: transparent;
    color: var(--danger);
    border: 1px solid var(--border);
  }
  .btn-delete:hover { background: #fef2f2; border-color: var(--danger); }

  /* ---- Table ---- */
  .issues-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }
  .issues-table th {
    text-align: left;
    padding: 12px 16px;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border);
    background: #f8fafc;
  }
  .issues-table td {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
  }
  .issues-table tbody tr { transition: background .1s; }
  .issues-table tbody tr:hover { background: #f8fafc; }
  .issues-table tbody tr:last-child td { border-bottom: none; }
  .ref-cell {
    font-weight: 600;
    font-family: "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 13px;
  }
  .notes-cell {
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-secondary);
  }

  /* ---- Status badge ---- */
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }
  .status-badge::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
  }
  .status-badge.open   { background: var(--open-bg);   color: var(--open-dot); }
  .status-badge.closed { background: var(--closed-bg); color: var(--closed-dot); }

  /* ---- Actions cell ---- */
  .actions { display: flex; gap: 8px; }

  /* ---- Empty state ---- */
  .empty-state {
    text-align: center;
    padding: 48px 24px;
    color: var(--text-secondary);
  }
  .empty-state .icon { font-size: 40px; margin-bottom: 12px; }

  /* ---- Responsive ---- */
  @media (max-width: 700px) {
    .summary { grid-template-columns: 1fr; }
    .form-grid { grid-template-columns: 1fr; }
    .issues-table { font-size: 12px; }
    .issues-table th, .issues-table td { padding: 8px; }
    .notes-cell { max-width: 120px; }
  }
</style>
</head>
<body>
<div class="container">

  <!-- ===== Header ===== -->
  <div class="header">
    <div>
      <h1>Issue Tracker</h1>
      <p>Track and manage shipment issues across your lanes</p>
    </div>
    <div class="mode-badge checking" id="modeBadge">Checking&hellip;</div>
  </div>

  <!-- ===== Local mode notice (hidden by default) ===== -->
  <div class="notice local" id="localNotice" style="display:none">
    <strong>Local test mode</strong> &mdash; data is stored in memory and
    will be lost when the app restarts. Deploy to Substrait to save issues
    permanently.
  </div>

  <!-- ===== Summary bar ===== -->
  <div class="summary">
    <div class="stat-card open">
      <div class="label">Open</div>
      <div class="value" id="openCount">&mdash;</div>
    </div>
    <div class="stat-card closed">
      <div class="label">Closed</div>
      <div class="value" id="closedCount">&mdash;</div>
    </div>
    <div class="stat-card total">
      <div class="label">Total</div>
      <div class="value" id="totalCount">&mdash;</div>
    </div>
  </div>

  <!-- ===== Add issue form ===== -->
  <div class="card">
    <div class="card-header">Add New Issue</div>
    <div class="card-body">
      <form id="issueForm" autocomplete="off">
        <div class="form-grid">
          <div class="form-group">
            <label for="reference">Reference</label>
            <input type="text" id="reference" placeholder="SBX00123" required>
          </div>
          <div class="form-group">
            <label for="lane">Lane</label>
            <input type="text" id="lane" placeholder="SG&#8594;MY" required>
          </div>
          <div class="form-group">
            <label for="issue_type">Issue Type</label>
            <input type="text" id="issue_type" placeholder="customs hold"
                   list="typeList" required>
            <datalist id="typeList">
              <option value="customs hold">
              <option value="delay">
              <option value="damage">
              <option value="missing item">
              <option value="documentation">
              <option value="payment">
              <option value="other">
            </datalist>
          </div>
          <div class="form-group">
            <label for="owner">Owner</label>
            <input type="text" id="owner" placeholder="Ali" required>
          </div>
          <div class="form-group">
            <label for="status">Status</label>
            <select id="status">
              <option value="open">Open</option>
              <option value="closed">Closed</option>
            </select>
          </div>
          <div class="form-group" style="grid-column:1/-1">
            <label for="notes">Notes</label>
            <input type="text" id="notes"
                   placeholder="waiting on HS code from shipper">
          </div>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn btn-primary">+ Add Issue</button>
        </div>
      </form>
    </div>
  </div>

  <!-- ===== Issues table ===== -->
  <div class="card">
    <div class="card-header">All Issues</div>
    <div id="tableBody"><!-- Rendered by JavaScript --></div>
  </div>

</div>

<script>
  /* ===== API helpers ===== */
  /* Each function calls the backend API and returns the JSON response. */

  async function fetchIssues() {
    const res = await fetch('/api/issues');
    if (!res.ok) throw new Error('Failed to load issues');
    return res.json();
  }

  async function createIssue(data) {
    const res = await fetch('/api/issues', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Failed to create issue');
    }
    return res.json();
  }

  async function toggleStatus(id, currentStatus) {
    const newStatus = currentStatus === 'open' ? 'closed' : 'open';
    const res = await fetch('/api/issues/' + id + '/status', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Failed to update status');
    }
    return res.json();
  }

  async function deleteIssue(id) {
    const res = await fetch('/api/issues/' + id, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Failed to delete issue');
    }
    return res.json();
  }

  /* ===== Rendering ===== */

  function renderTable(issues) {
    const container = document.getElementById('tableBody');

    if (issues.length === 0) {
      container.innerHTML =
        '<div class="empty-state">' +
          '<div class="icon">\u{1F4CB}</div>' +
          '<p>No issues yet. Add one above to get started.</p>' +
        '</div>';
      return;
    }

    // Build one table row per issue
    var rows = issues.map(function(issue) {
      return '<tr>' +
        '<td class="ref-cell">' + esc(issue.reference) + '</td>' +
        '<td>' + esc(issue.lane) + '</td>' +
        '<td>' + esc(issue.issue_type) + '</td>' +
        '<td><span class="status-badge ' + esc(issue.status) + '">' +
          esc(issue.status) + '</span></td>' +
        '<td>' + esc(issue.owner) + '</td>' +
        '<td class="notes-cell" title="' + esc(issue.notes) + '">' +
          esc(issue.notes || '\u2014') + '</td>' +
        '<td><div class="actions">' +
          '<button class="btn btn-sm btn-toggle' +
            (issue.status === 'closed' ? ' closed' : '') +
            '" onclick="handleToggle(' + issue.id + ',\'' + issue.status + '\')">' +
            (issue.status === 'open' ? 'Close' : 'Reopen') +
          '</button>' +
          '<button class="btn btn-sm btn-delete" onclick="handleDelete(' +
            issue.id + ')">Delete</button>' +
        '</div></td>' +
      '</tr>';
    }).join('');

    container.innerHTML =
      '<table class="issues-table"><thead><tr>' +
        '<th>Reference</th><th>Lane</th><th>Type</th>' +
        '<th>Status</th><th>Owner</th><th>Notes</th><th>Actions</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function updateSummary(issues) {
    var open = issues.filter(function(i) { return i.status === 'open'; }).length;
    var closed = issues.filter(function(i) { return i.status === 'closed'; }).length;
    document.getElementById('openCount').textContent = open;
    document.getElementById('closedCount').textContent = closed;
    document.getElementById('totalCount').textContent = issues.length;
  }

  /* ===== Event handlers ===== */

  async function loadAll() {
    try {
      var issues = await fetchIssues();
      renderTable(issues);
      updateSummary(issues);
    } catch (e) {
      document.getElementById('tableBody').innerHTML =
        '<div class="empty-state">' +
          '<div class="icon">\u26A0\uFE0F</div>' +
          '<p>' + esc(e.message) + '</p>' +
        '</div>';
    }
  }

  document.getElementById('issueForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    var form = e.target;
    var data = {
      reference:   form.reference.value.trim(),
      lane:        form.lane.value.trim(),
      issue_type:  form.issue_type.value.trim(),
      status:      form.status.value,
      owner:       form.owner.value.trim(),
      notes:       form.notes.value.trim(),
    };
    try {
      await createIssue(data);
      form.reset();   // clear the form
      await loadAll(); // refresh the table
    } catch (e) {
      alert(e.message);
    }
  });

  async function handleToggle(id, currentStatus) {
    try {
      await toggleStatus(id, currentStatus);
      await loadAll();
    } catch (e) {
      alert(e.message);
    }
  }

  async function handleDelete(id) {
    if (!confirm('Delete this issue? This cannot be undone.')) return;
    try {
      await deleteIssue(id);
      await loadAll();
    } catch (e) {
      alert(e.message);
    }
  }

  /* ===== Utility ===== */

  // Escape user input to prevent XSS (cross-site scripting)
  function esc(str) {
    var div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML.replace(/"/g, '&quot;');
  }

  /* ===== Init: check storage mode, then load issues ===== */

  async function init() {
    try {
      var res = await fetch('/api/info');
      var info = await res.json();
      var badge = document.getElementById('modeBadge');
      var notice = document.getElementById('localNotice');

      if (info.mode && info.mode.indexOf('local') !== -1) {
        badge.textContent = 'Local Test Mode';
        badge.className = 'mode-badge local';
        notice.style.display = 'block';
      } else {
        badge.textContent = 'Live';
        badge.className = 'mode-badge live';
        notice.style.display = 'none';
      }
    } catch (e) {
      document.getElementById('modeBadge').textContent = 'Offline';
    }
    await loadAll();
  }

  init();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def homepage():
    """Serve the main web page."""
    return PAGE
