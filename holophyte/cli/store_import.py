from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import store.read
import store.schema

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", re.S)
# The one column a remap offsets; a foreign key moves with its rewiring.
_ID_COLUMN = re.compile(r"^\s*id\s+INTEGER PRIMARY KEY\b", re.M)


def schema_tables():
    ddl = store.schema.SCHEMA + store.schema._INTERVENTIONS_DDL
    return [(name, "id" if _ID_COLUMN.search(body) else None)
            for name, body in _TABLE.findall(ddl)]


class VersionMismatch(SystemExit):
    def __init__(self, source, dest):
        self.source, self.dest = source, dest
        super().__init__(
            f"[holo2] import refused: source store is schema {source}, "
            f"destination store is schema {dest}; both must be at the same "
            "version -- nothing was written")


@dataclass(frozen=True)
class TablePlan:
    table: str
    rows: int
    min_id: int | None
    max_id: int | None
    next_id: int | None
    offset: int | None
    sha256: str


@dataclass(frozen=True)
class Plan:
    source: str
    version: int
    tables: list[TablePlan]


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def checksum(conn, table, id_column):
    """Rows in a fixed order, so the same rows hash the same in any file."""
    cursor = conn.execute(f'SELECT * FROM "{table}"'
                          + (f' ORDER BY "{id_column}"' if id_column else ""))
    columns = [d[0] for d in cursor.description]
    lines = [json.dumps(dict(zip(columns, row)), sort_keys=True)
             for row in cursor]
    if id_column is None:
        lines.sort()
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode() + b"\n")
    return digest.hexdigest()


def _table_plan(source_conn, dest_conn, table, id_column):
    rows = source_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    min_id = max_id = next_id = offset = None
    if id_column is not None:
        min_id, max_id = source_conn.execute(
            f'SELECT MIN("{id_column}"), MAX("{id_column}") FROM "{table}"'
        ).fetchone()
        offset = dest_conn.execute(
            f'SELECT COALESCE(MAX("{id_column}"), 0) FROM "{table}"'
        ).fetchone()[0]
        next_id = offset + 1
    return TablePlan(table, rows, min_id, max_id, next_id, offset,
                     checksum(source_conn, table, id_column))


@contextlib.contextmanager
def _snapshot(conn):
    """A transaction the caller already holds is theirs, and left open."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


def plan(source_conn, dest_conn):
    with _snapshot(source_conn), _snapshot(dest_conn):
        return _plan(source_conn, dest_conn)


def _plan(source_conn, dest_conn):
    version = schema_version(source_conn)
    dest_version = schema_version(dest_conn)
    if version != dest_version:
        raise VersionMismatch(version, dest_version)
    source = source_conn.execute("PRAGMA database_list").fetchone()[2]
    return Plan(source, version,
                [_table_plan(source_conn, dest_conn, table, column)
                 for table, column in schema_tables()])


def render(plan):
    lines = []
    for t in plan.tables:
        if t.offset is None:
            ids = "ids n/a  offset n/a"
        elif t.rows:
            ids = (f"ids {t.min_id}..{t.max_id}  next {t.next_id}"
                   f"  offset {t.offset}")
        else:
            ids = f"ids none  next {t.next_id}  offset {t.offset}"
        lines.append(f"{t.table}  rows {t.rows}  {ids}  sha256 {t.sha256}")
    lines.append(f"source {plan.source}  schema {plan.version}")
    return lines


def dry_run(target, source_path, out=None):
    out = out or sys.stdout
    source_path = Path(source_path)
    for path, role in ((source_path, "source"),
                       (target.store_path, "destination")):
        if not path.is_file():
            raise SystemExit(f"[holo2] no {role} store at {path}")
    source_conn = store.read.open_readonly(source_path)
    try:
        dest_conn = store.read.open_readonly(target.store_path)
        try:
            print("\n".join(render(plan(source_conn, dest_conn))), file=out)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
