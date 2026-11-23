#!/usr/bin/env python3
"""IT Service Desk - ticketing with SLA tracking and an immutable audit trail.

Zero dependencies (Python 3.8+). Run:  python3 server.py  ->  http://localhost:8000
"""
import csv
import io
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TICKETS_DB", os.path.join(HERE, "tickets.db"))

# ---- Business rules (edit here) -------------------------------------------
SLA_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}  # P1 = 4h ... P4 = 3 days
PRIORITY_LABELS = {"P1": "Critical", "P2": "High", "P3": "Medium", "P4": "Low"}
CATEGORIES = [
    "Hardware", "Software", "Network / VPN", "Email & Calendar",
    "Account & Access", "Security", "Printing", "Other",
]
AT_RISK_FRACTION = 0.25  # amber when <= 25% of the SLA window remains
STATUSES = ["Open", "In Progress", "Resolved", "Closed"]
# Allowed transitions. Resolved -> In Progress is a reopen; Closed is final.
TRANSITIONS = {
    "Open": ["In Progress"],
    "In Progress": ["Resolved"],
    "Resolved": ["Closed", "In Progress"],
    "Closed": [],
}
MAX_DESC = 5000
ALLOWED_ORIGINS = {"null", "http://localhost:8000", "http://127.0.0.1:8000"}  # "null" = file://

SEED_USERS = [  # demo identities - replace with SSO/AD in production
    ("Alice Tan", "alice@company.com", "requester"),
    ("Bob Lim", "bob@company.com", "requester"),
    ("Chitra Nair", "chitra@company.com", "requester"),
    ("Dev Patel", "dev.patel@company.com", "agent"),
    ("Erin Walsh", "erin.walsh@company.com", "agent"),
]


# ---- Time helpers -----------------------------------------------------------
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---- Database ---------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL CHECK(role IN('requester','agent')));
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL, description TEXT NOT NULL,
  priority TEXT NOT NULL CHECK(priority IN('P1','P2','P3','P4')),
  status TEXT NOT NULL DEFAULT 'Open',
  requester_id INTEGER NOT NULL REFERENCES users(id),
  assignee_id INTEGER REFERENCES users(id),
  sla_hours INTEGER NOT NULL,
  created_at TEXT NOT NULL, sla_due_at TEXT NOT NULL,
  resolved_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS ticket_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  event_type TEXT NOT NULL,             -- created | status_change
  from_status TEXT, to_status TEXT NOT NULL,
  actor_id INTEGER NOT NULL, actor_name TEXT NOT NULL, actor_role TEXT NOT NULL,
  at TEXT NOT NULL, note TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON ticket_events(ticket_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON ticket_events
  BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON ticket_events
  BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
"""


def db():
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    c = db()
    c.executescript(SCHEMA)
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        c.executemany("INSERT INTO users(name,email,role) VALUES(?,?,?)", SEED_USERS)
    c.close()


# ---- SLA computation --------------------------------------------------------
def sla_view(t):
    """Derive SLA state for a ticket row. The clock stops when it is Resolved."""
    due = parse(t["sla_due_at"])
    total = t["sla_hours"] * 3600
    if t["status"] in ("Resolved", "Closed"):
        stop = parse(t["resolved_at"]) if t["resolved_at"] else now()
        met = stop <= due
        return {"state": "met" if met else "breached_closed",
                "remaining_seconds": int((due - stop).total_seconds())}
    remaining = int((due - now()).total_seconds())
    if remaining <= 0:
        state = "breached"
    elif remaining <= total * AT_RISK_FRACTION:
        state = "at_risk"
    else:
        state = "ok"
    return {"state": state, "remaining_seconds": remaining}


def ticket_dict(t):
    d = {k: t[k] for k in ("id", "category", "description", "priority", "status",
                           "sla_hours", "created_at", "sla_due_at", "resolved_at",
                           "closed_at", "requester_id", "assignee_id",
                           "requester_name", "assignee_name")}
    d["priority_label"] = PRIORITY_LABELS[t["priority"]]
    d["sla"] = sla_view(t)
    return d


TICKET_SELECT = """SELECT t.*, r.name AS requester_name, a.name AS assignee_name
 FROM tickets t JOIN users r ON r.id=t.requester_id
 LEFT JOIN users a ON a.id=t.assignee_id"""


# ---- HTTP handler -----------------------------------------------------------
class HttpError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "ITDesk/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing
    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # CORS so index.html opened from disk (Origin: null) can use this server.
        # Only an allow-list is echoed back; sign-in there is by bearer token, not cookie.
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj), extra=extra)

    def _body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise HttpError(415, "Content-Type must be application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise HttpError(413, "Body too large")
        try:
            v = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise HttpError(400, "Invalid JSON")
        if not isinstance(v, dict):
            raise HttpError(400, "Expected a JSON object")
        return v

    def _token(self):
        """Session token from 'Authorization: Bearer' (file:// page) or the 'sid' cookie."""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        raw = self.headers.get("Cookie")
        if raw:
            c = cookies.SimpleCookie()
            try:
                c.load(raw)
            except cookies.CookieError:
                return None
            if "sid" in c:
                return c["sid"].value
        return None

    def _user(self, conn):
        tok = self._token()
        if not tok:
            return None
        return conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (tok,)).fetchone()

    def do_OPTIONS(self):  # CORS preflight
        self._send(204, b"", "text/plain")

    def _require(self, conn, role=None):
        u = self._user(conn)
        if not u:
            raise HttpError(401, "Not signed in")
        if role and u["role"] != role:
            raise HttpError(403, "Support agents only")
        return u

    def _dispatch(self, method):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        conn = db()
        try:
            self._route(conn, method, path, qs)
        except HttpError as e:
            self._json({"error": e.msg}, e.code)
        except Exception as e:  # noqa
            sys.stderr.write("ERROR %r\n" % (e,))
            self._json({"error": "Internal error"}, 500)
        finally:
            conn.close()

    do_GET = lambda self: self._dispatch("GET")
    do_POST = lambda self: self._dispatch("POST")

    def _static(self, path):
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        self._send(404, "Not found", "text/plain")

    # -- routes
    def _route(self, conn, method, path, qs):
        seg = path.strip("/").split("/")  # ['api', ...]
        if method == "GET" and path == "/api/meta":
            return self._json({
                "categories": CATEGORIES, "statuses": STATUSES,
                "transitions": TRANSITIONS,
                "priorities": [{"code": p, "label": PRIORITY_LABELS[p],
                                "sla_hours": h} for p, h in SLA_HOURS.items()],
                "at_risk_fraction": AT_RISK_FRACTION})
        if method == "GET" and path == "/api/users":  # demo login picker
            rows = conn.execute("SELECT id,name,email,role FROM users ORDER BY role,name")
            return self._json([dict(r) for r in rows])
        if method == "GET" and path == "/api/me":
            u = self._user(conn)
            return self._json({"user": dict(u) if u else None})
        if method == "POST" and path == "/api/login":
            b = self._body()
            u = conn.execute("SELECT * FROM users WHERE id=?", (b.get("user_id"),)).fetchone()
            if not u:
                raise HttpError(400, "Unknown user")
            tok = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, u["id"], iso(now())))
            ck = "sid=%s; HttpOnly; SameSite=Lax; Path=/" % tok
            return self._json({"user": dict(u), "token": tok}, extra=[("Set-Cookie", ck)])
        if method == "POST" and path == "/api/register":
            # Self-registration always creates an employee (requester), never an agent.
            name = " ".join(str(self._body().get("name") or "").split())
            if not 2 <= len(name) <= 60:
                raise HttpError(400, "Name must be 2-60 characters")
            if conn.execute("SELECT 1 FROM users WHERE lower(name)=lower(?)", (name,)).fetchone():
                raise HttpError(409, "That name is already registered - pick it from the list")
            slug = "".join(ch for ch in name.lower().replace(" ", ".") if ch.isalnum() or ch == ".") or "user"
            email, n = "%s@company.com" % slug, 1
            while conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                n += 1
                email = "%s%d@company.com" % (slug, n)
            cur = conn.execute("INSERT INTO users(name,email,role) VALUES(?,?,'requester')", (name, email))
            u = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            tok = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, u["id"], iso(now())))
            ck = "sid=%s; HttpOnly; SameSite=Lax; Path=/" % tok
            return self._json({"user": dict(u), "token": tok}, 201, extra=[("Set-Cookie", ck)])
        if method == "POST" and path == "/api/logout":
            tok = self._token()
            if tok:
                conn.execute("DELETE FROM sessions WHERE token=?", (tok,))
            return self._json({"ok": True}, extra=[
                ("Set-Cookie", "sid=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")])

        if seg[:2] == ["api", "tickets"]:
            u = self._require(conn)
            if len(seg) == 2 and method == "POST":
                return self._create_ticket(conn, u)
            if len(seg) == 2 and method == "GET":
                return self._list_tickets(conn, u, qs)
            if len(seg) == 3 and method == "GET" and seg[2].isdigit():
                return self._get_ticket(conn, u, int(seg[2]))
            if len(seg) == 4 and seg[3] == "status" and method == "POST" and seg[2].isdigit():
                return self._change_status(conn, u, int(seg[2]))
        if method == "GET" and path == "/api/audit.csv":
            self._require(conn, "agent")
            return self._audit_csv(conn)
        raise HttpError(404, "Not found")

    def _create_ticket(self, conn, u):
        b = self._body()
        category = b.get("category")
        priority = b.get("priority")
        desc = (b.get("description") or "").strip()
        if category not in CATEGORIES:
            raise HttpError(400, "Invalid category")
        if priority not in SLA_HOURS:
            raise HttpError(400, "Invalid priority")
        if not desc:
            raise HttpError(400, "Description is required")
        if len(desc) > MAX_DESC:
            raise HttpError(400, "Description too long (max %d)" % MAX_DESC)
        created = now()
        hours = SLA_HOURS[priority]
        due = created + timedelta(hours=hours)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO tickets(category,description,priority,status,requester_id,"
                "sla_hours,created_at,sla_due_at) VALUES(?,?,?,?,?,?,?,?)",
                (category, desc, priority, "Open", u["id"], hours, iso(created), iso(due)))
            tid = cur.lastrowid
            conn.execute(
                "INSERT INTO ticket_events(ticket_id,event_type,from_status,to_status,"
                "actor_id,actor_name,actor_role,at,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, "created", None, "Open", u["id"], u["name"], u["role"], iso(created),
                 "Ticket raised; SLA %dh (due %s)" % (hours, iso(due))))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        t = conn.execute(TICKET_SELECT + " WHERE t.id=?", (tid,)).fetchone()
        self._json(ticket_dict(t), 201)

    def _list_tickets(self, conn, u, qs):
        where, args = [], []
        if u["role"] == "requester":
            where.append("t.requester_id=?")
            args.append(u["id"])
        view = (qs.get("view") or ["active"])[0]
        if view == "active":
            where.append("t.status IN ('Open','In Progress')")
        rows = conn.execute(
            TICKET_SELECT + (" WHERE " + " AND ".join(where) if where else ""), args).fetchall()
        out = [ticket_dict(r) for r in rows]
        # Queue order: least time remaining before breach first; overdue (negative) on top.
        # Finished tickets (Resolved/Closed) sink below active ones, newest first.
        active = sorted([t for t in out if t["status"] in ("Open", "In Progress")],
                        key=lambda t: t["sla"]["remaining_seconds"])
        done = sorted([t for t in out if t["status"] not in ("Open", "In Progress")],
                      key=lambda t: t["created_at"], reverse=True)
        self._json(active + done)

    def _get_ticket(self, conn, u, tid):
        t = conn.execute(TICKET_SELECT + " WHERE t.id=?", (tid,)).fetchone()
        if not t or (u["role"] == "requester" and t["requester_id"] != u["id"]):
            raise HttpError(404, "Ticket not found")
        ev = conn.execute("SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id", (tid,))
        d = ticket_dict(t)
        d["events"] = [dict(e) for e in ev]
        self._json(d)

    def _change_status(self, conn, u, tid):
        if u["role"] != "agent":
            raise HttpError(403, "Support agents only")
        b = self._body()
        new = b.get("status")
        note = (b.get("note") or "").strip()[:500] or None
        if new not in STATUSES:
            raise HttpError(400, "Invalid status")
        conn.execute("BEGIN IMMEDIATE")  # serialise: check + update + audit are atomic
        try:
            t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not t:
                raise HttpError(404, "Ticket not found")
            if new not in TRANSITIONS[t["status"]]:
                raise HttpError(409, "Cannot move %s -> %s" % (t["status"], new))
            ts = iso(now())
            sets, vals = ["status=?"], [new]
            if new == "In Progress":
                sets.append("assignee_id=?")
                vals.append(u["id"])
                if t["resolved_at"]:  # reopened: SLA clock restarts running again
                    sets.append("resolved_at=NULL")
            if new == "Resolved":
                sets.append("resolved_at=?")
                vals.append(ts)
            if new == "Closed":
                sets.append("closed_at=?")
                vals.append(ts)
            conn.execute("UPDATE tickets SET %s WHERE id=?" % ",".join(sets), vals + [tid])
            conn.execute(
                "INSERT INTO ticket_events(ticket_id,event_type,from_status,to_status,"
                "actor_id,actor_name,actor_role,at,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, "status_change", t["status"], new, u["id"], u["name"], u["role"], ts, note))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self._get_ticket(conn, u, tid)

    def _audit_csv(self, conn):
        rows = conn.execute("SELECT * FROM ticket_events ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["event_id", "ticket_id", "event_type", "from_status", "to_status",
                    "actor", "actor_role", "timestamp_utc", "note"])
        for r in rows:
            note = r["note"] or ""
            if note[:1] in "=+-@":  # neutralise spreadsheet formula injection
                note = "'" + note
            w.writerow([r["id"], r["ticket_id"], r["event_type"], r["from_status"] or "",
                        r["to_status"], r["actor_name"], r["actor_role"], r["at"], note])
        self._send(200, buf.getvalue(), "text/csv", [
            ("Content-Disposition", 'attachment; filename="ticket_audit_log.csv"')])


def main():
    init_db()
    port = int(os.environ.get("PORT", 8000))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("IT Service Desk running at http://localhost:%d" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
