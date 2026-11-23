# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

IT Service Desk: employees raise tickets (category, description, priority P1–P4), the system assigns an SLA due time, agents work a queue sorted by time remaining before breach, and every status change is written to an audit log. Two files, no dependencies, no build step, no tests, no linter, no git repo:

- `server.py` – Python 3.8+ standard library only (`http.server` + `sqlite3`).
- `index.html` – the entire frontend (HTML, CSS and JS in one file).

## Run

```bash
python3 server.py            # http://localhost:8000 ; env: PORT=8001, TICKETS_DB=/path/to.db
```

Or open `index.html` directly (file://) for the standalone mode. The server reads `index.html` on every request, so frontend edits need only a browser refresh; `server.py` edits need a restart (Ctrl+C, rerun). If port 8000 is "already in use", an earlier server is still running.

There is no test suite. To exercise the backend, run it against a throwaway DB (`TICKETS_DB=/tmp/x.db PORT=8765`) and drive it with `curl` using a cookie jar (`POST /api/login {"user_id":4}` for an agent, `1` for an employee). To test file-mode frontend logic without a real file:// page, serve the app over http and substitute `location.protocol === 'file:'` with `true` in the fetched HTML (browsers block storage for `data:` snapshots).

## Architecture

**The business logic exists twice and must be kept in sync.** `index.html` runs in two modes chosen by `LOCAL = location.protocol === 'file:'`:

- http(s): `api()` calls `fetch` against `server.py` (shared SQLite DB).
- file://: `api()` routes to `localApi()`, a JS reimplementation of the server's routes backed by `localStorage` (`itdesk_v1`); the signed-in user lives in `sessionStorage` so each tab can be a different person.

Any change to SLA hours, categories, priorities, status transitions, validation, registration rules or the queue ordering has to be made in both `server.py` (constants at the top, route handlers in `Handler`) and `index.html` (the `L` constants object and `localApi`). The UI code itself (render/draw functions) is shared and only talks to `api()`.

Key rules, implemented identically in both places:

- SLA hours: P1=4, P2=8, P3=24, P4=72. Due time is fixed at creation. The SLA clock stops at `resolved_at`; reopening (Resolved → In Progress) clears it, so the clock runs again against the original due time. State is derived, never stored: `breached` (≤0 remaining), `at_risk` (≤25% of window left), `ok`, and `met`/`breached_closed` once resolved.
- Queue order: active tickets (Open/In Progress) ascending by remaining seconds, so overdue tickets are first; finished tickets sink below, newest first.
- Transitions: Open → In Progress → Resolved → Closed, plus Resolved → In Progress. Enforced server-side; only agents may change status. Moving to In Progress assigns the acting agent.
- Every ticket creation and status change appends a `ticket_events` row (actor name/role, UTC timestamp, optional note). In server mode SQLite triggers abort UPDATE/DELETE on that table, and status change + audit insert happen in one `BEGIN IMMEDIATE` transaction. File mode has no tamper protection.
- Self-registration (`+ New User` in the sign-in dropdown, `/api/register`) always creates a `requester`, never an agent; names are unique case-insensitively and emails are generated as `<slug>@company.com`.
- Requesters can only read their own tickets (404 for others); agents see everything and can export the audit log as CSV (formula-injection characters are neutralised in both implementations).

Auth is a demo: sign-in is a dropdown of seeded users (`SEED_USERS` in `server.py`, duplicated in `ldb()` in `index.html`), with a cookie session in server mode. It is meant to be replaced by SSO.

Frontend notes: all user-supplied text is inserted through the `esc()` helper; keep doing so when adding templates. Countdowns tick client-side every second (`tick()`), and the list refetches every 30 s to re-sort.
