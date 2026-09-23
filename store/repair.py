"""store.repair: rewrite foreign keys that name a dropped table (KO-665).

A table rebuild that renames the live table away leaves every key pointing
at it following the rename, so a later DROP leaves `runs` referencing
`interventions_old` and every insert into `runs` fails. The operator's hand
repair was an edit of `sqlite_master`; this is that edit with a dry run, a
backup and the decision recorded first, re-exported from `store.operate`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from .schema import _transaction

# What a scan of DDL steps over whole, so text inside it is never read as a
# clause: string literals, quoted identifiers and comments.
_OPAQUE = r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/"""


def _references(name):
    """Match a `REFERENCES <name>` clause, or one opaque token to step over.

    The clause is tried first at each position, so its quoted table name is
    read as part of it; anywhere else a quoted token is skipped whole."""
    return re.compile(
        r"(\bREFERENCES\s+)(?:([\"`\[])" + re.escape(name) + r"([\"`\]])"
        r"|" + re.escape(name) + r"(?![\w$]))|" + _OPAQUE, re.I | re.S)


def _rewrite(sql, missing, target):
    """Return `sql` with each `REFERENCES missing` clause naming `target`,
    and how many clauses that was."""
    count = 0

    def replace(match):
        nonlocal count
        if match.group(1) is None:
            return match.group(0)
        count += 1
        return (match.group(1) + (match.group(2) or "") + target
                + (match.group(3) or ""))
    return _references(missing).sub(replace, sql), count


def _dangling(conn):
    """Each foreign key naming a missing table, with its unambiguous target."""
    tables = {name.lower(): (name, sql) for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    found = []
    for name, sql in tables.values():
        for row in conn.execute(f'PRAGMA foreign_key_list("{name}")'):
            missing = row[2]
            if missing.lower() in tables:
                continue
            base = re.sub(r"_(old|new)$", "", missing, flags=re.I).lower()
            target = None
            if (base != missing.lower() and base in tables
                    and _rewrite(sql, missing, base)[1]):
                target = tables[base][0]
            found.append((name, row[3], missing, target))
    return found


def _backup(conn):
    """Copy a file-backed store beside itself; return the copy's path."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        return None
    backup = f"{path}.pre-repair-{int(time.time() * 1000)}"
    copy = sqlite3.connect(backup)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return backup


def repair_references(conn, dry_run=True):
    """Find foreign keys naming a missing table; rewrite the unambiguous ones.

    Return a (table, column, missing, target) tuple per dangling key.
    `target` is the table named `missing` less an `_old` or `_new` suffix,
    or None when there is no such table (or the DDL does not spell the
    reference plainly); a key with no target is reported, never rewritten.

    With `dry_run` false and something to rewrite: back the store's file
    up beside it, then in one transaction record a `migrate` project
    intervention, rewrite each table's DDL under `writable_schema`, bump
    the schema cookie, and run `integrity_check` and `foreign_key_check`
    over the whole store. Anything unclean raises `sqlite3.DatabaseError`
    and rolls the whole transaction back, intervention included.
    """
    from .operate import record_project_intervention
    found = _dangling(conn)
    fixes = [ref for ref in found if ref[3] is not None]
    if dry_run or not fixes:
        return found
    backup = _backup(conn)
    with _transaction(conn):
        record_project_intervention(conn, "migrate", json.dumps(
            {"repair_references": fixes, "backup": backup}))
        ddl = {}
        for table, _column, missing, target in fixes:
            if table not in ddl:
                ddl[table] = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table'"
                    " AND name = ?", (table,)).fetchone()[0]
            ddl[table] = _rewrite(ddl[table], missing, target)[0]
        (version,) = conn.execute("PRAGMA schema_version").fetchone()
        conn.execute("PRAGMA writable_schema = ON")
        try:
            for table, sql in ddl.items():
                conn.execute("UPDATE sqlite_master SET sql = ?"
                             " WHERE type = 'table' AND name = ?", (sql, table))
            conn.execute(f"PRAGMA schema_version = {version + 1:d}")
        finally:
            conn.execute("PRAGMA writable_schema = OFF")
        problems = [row for row in conn.execute("PRAGMA integrity_check")
                    if row != ("ok",)]
        problems += conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.DatabaseError(
                f"repair left the store unclean, rolled back: {problems}")
    return found
