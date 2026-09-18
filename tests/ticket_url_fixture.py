"""Ticket URL acceptance scenarios shared by the pinned test modules."""
import sqlite3
import tempfile
from pathlib import Path

import linear_provider
import store
import store.schema
import store.tickets
from holophyte.board import mirror_task

MIN = 60_000


def assert_mirror_url(case):
    task = {"id": "KO-1", "issue_id": "issue-1", "title": "ticket",
            "body": "", "budget_min": 25,
            "url": "https://linear.app/team/issue/KO-1/original"}
    ticket_id = mirror_task(case.conn, case.project, task)
    case.assertEqual(case.conn.execute(
        "SELECT url FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone(), (task["url"],))
    for url in ("https://linear.app/team/issue/KO-1/renamed", None):
        task["url"] = url
        case.assertEqual(mirror_task(case.conn, case.project, task), ticket_id)
        case.assertEqual(case.conn.execute(
            "SELECT url FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone(), (url,))


def assert_schema_url(case):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "store.db"
        conn = sqlite3.connect(path)
        previous = "\n".join(line for line in store.schema.SCHEMA.splitlines()
                             if not line.strip().startswith("url "))
        conn.executescript(previous)
        project = store.tickets.ensure_project(conn, "team", "/repo")
        conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                     " linearIdentifier, title, status, mirroredAt, affinity)"
                     " VALUES (?, 'issue', 'KO-1', 'old', 'ready', 1, 'any')",
                     (project,))
        conn.execute("PRAGMA user_version = 22")
        conn.commit()
        conn.close()
        conn = store.open(path)
        try:
            case.assertIn("url", [r[1] for r in conn.execute(
                "PRAGMA table_info(tickets)")])
            case.assertEqual(conn.execute("SELECT url FROM tickets").fetchone(),
                             (None,))
            case.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (23,))
        finally:
            conn.close()


def assert_api_url(case):
    case.seed()
    url = "https://linear.app/team/issue/KO-7/ticket"
    conn = store.open(case.db)
    try:
        project = store.tickets.ensure_project(conn, "team-1", case.target)
        store.tickets.mirror_ticket(
            conn, project, "issue-7", "KO-7", "ticket 7", url=url)
        store.heartbeat(conn, case.run, now=case.now - 20 * MIN)
    finally:
        conn.close()
    case.start()
    for path, field in (("/status", "runs"), ("/attention", "items")):
        code, _, body = case.request("GET", path)
        case.assertEqual(code, 200)
        row = next(r for r in body[field] if r.get("ticket") == "KO-7")
        case.assertEqual(row["ticket_url"], url)
    code, _, body = case.request("GET", f"/runs/{case.run}")
    case.assertEqual(code, 200)
    case.assertEqual(body["run"]["ticket_url"], url)
    code, _, body = case.request("GET", "/board")
    case.assertEqual(code, 200)
    row = next(t for c in body["columns"] for t in c["tickets"]
               if t["ticket"] == "KO-7")
    case.assertEqual(row["ticket_url"], url)
    conn = store.open(case.db)
    try:
        store.release(conn, case.run, "merged", now=case.now)
    finally:
        conn.close()
    for path in ("/runs", "/shipped"):
        code, _, body = case.request("GET", path)
        case.assertEqual(code, 200)
        case.assertEqual(body["rows"][0]["ticket_url"], url)


def assert_provider_url(self):
    self.seed("KO-1")
    url = "https://linear.app/team/issue/KO-1/a-ticket"
    self.board.issues["KO-1"]["url"] = url
    self.assertEqual(self.provider.ready_issues()[0]["url"], url)
    self.assertEqual(self.provider.fetch_task("KO-1")["url"], url)
    self.assertIn("url", linear_provider.READY_QUERY.split())
    self.assertIn("url", linear_provider.ISSUE_QUERY.split())
