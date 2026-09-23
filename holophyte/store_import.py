"""The dry run of folding another target's store into this one (KO-595).

`plan()` reads two stores and says, per table the schema declares, what an
import of the source into the destination would move: how many rows, which
ids, the offset a remap would add to them, and a sha256 of the rows, so the
later apply step can be checked against rows in and rows out. `render()` is
that plan as text, and `dry_run()` is `--import-store PATH --dry-run`'s whole
body: both stores open read-only and nothing is written to either.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import store.read
import store.schema

# `CREATE TABLE IF NOT EXISTS name (` ... `)` in the schema's DDL, the body
# up to the closing parenthesis at the start of a line.
_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", re.S)
# A surrogate `id` key, the one column a remap offsets. `sweepStrikes` is
# keyed by `runId`, a foreign key, which moves with foreign-key rewiring
# rather than with an offset of its own.
_ID_COLUMN = re.compile(r"^\s*id\s+INTEGER PRIMARY KEY\b", re.M)


def schema_tables():
    """`(table, id column or None)` for each table `store/schema.py` creates,
    in the order it creates them."""
    # `interventions` is declared apart from SCHEMA so its rebuild shares
    # the DDL; it is a table of the schema all the same.
    ddl = store.schema.SCHEMA + store.schema._INTERVENTIONS_DDL
    return [(name, "id" if _ID_COLUMN.search(body) else None)
            for name, body in _TABLE.findall(ddl)]


class VersionMismatch(SystemExit):
    """The two stores are at different schema versions; nothing is planned."""

    def __init__(self, source, dest):
        self.source, self.dest = source, dest
        super().__init__(
            f"[holo2] import refused: source store is schema {source}, "
            f"destination store is schema {dest}; both must be at the same "
            "version -- nothing was written")


@dataclass(frozen=True)
class TablePlan:
    """What an import would do with one table of the source."""

    table: str
    rows: int
    min_id: int | None
    max_id: int | None
    next_id: int | None  # the destination's next id; None without an id
    offset: int | None   # added to each source id; None without an id
    sha256: str


@dataclass(frozen=True)
class Plan:
    source: str   # the source store's file, as its connection names it
    version: int
    tables: list[TablePlan]


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def checksum(conn, table, id_column):
    """sha256 over the table's rows as JSON with sorted keys, one per line.

    In id order, or in the order of their JSON where there is no id, so the
    same rows in two files hash the same whatever order they were written."""
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
        # SQLite's next rowid for an INTEGER PRIMARY KEY is one past the
        # largest, so source id n lands at n + the destination's largest.
        offset = dest_conn.execute(
            f'SELECT COALESCE(MAX("{id_column}"), 0) FROM "{table}"'
        ).fetchone()[0]
        next_id = offset + 1
    return TablePlan(table, rows, min_id, max_id, next_id, offset,
                     checksum(source_conn, table, id_column))


def plan(source_conn, dest_conn):
    """The `Plan` for importing `source_conn`'s store into `dest_conn`'s.

    Reads only. Refuses with `VersionMismatch` when the two stores' schema
    versions differ, before reading a table."""
    version = schema_version(source_conn)
    dest_version = schema_version(dest_conn)
    if version != dest_version:
        raise VersionMismatch(version, dest_version)
    source = source_conn.execute("PRAGMA database_list").fetchone()[2]
    return Plan(source, version,
                [_table_plan(source_conn, dest_conn, table, column)
                 for table, column in schema_tables()])


def render(plan):
    """The plan as lines: one per table, then the source and its version."""
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
    """`--import-store PATH --dry-run`: print the plan and write nothing."""
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
