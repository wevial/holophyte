"""Project admission reads and the CLI's recorded operator commands."""
import store
from holophyte.config_tables import board_config
from holophyte.runs import open_store
from store.project_paths import canonical_projects


def held_line(conn, project):
    row = conn.execute(
        "SELECT repoPath, holdNote, admission FROM projects "
        "WHERE id = ? AND admission != 'enabled'",
        (project,)).fetchone()
    return f"[holo2] project {row[0]} {row[2]}: {row[1]}" if row else None


def lines(conn):
    return [f"[holo2] project {path} {admission}: {note}"
            for path, note, admission in conn.execute(
                "SELECT repoPath, holdNote, admission FROM projects "
                "WHERE admission != 'enabled' ORDER BY id")]


def state(conn, target):
    # A read-only daemon may start before the writer migrates admission in v28.
    if conn.execute("PRAGMA user_version").fetchone()[0] < 28:
        return "enabled", None
    paths = canonical_projects(conn)
    project = next((key for key, path in paths.items()
                    if path == str(target.path.resolve())), None)
    row = conn.execute("SELECT admission, holdNote FROM projects WHERE id = ?",
                       (project,)).fetchone()
    return row or ("enabled", None)


def change(target, holding, note):
    conn = open_store(target)
    try:
        paths = canonical_projects(conn)
        row = next(((key,) for key, path in paths.items()
                    if path == str(target.path.resolve())), None)
        if row is None:
            settings = board_config(target)
            if settings is None:
                raise ValueError("project has no store row or [board] configuration")
            project = store.ensure_project(conn, settings.team, target.path)
        else:
            project = row[0]
        (store.hold if holding else store.release_hold)(conn, project, note)
        print(held_line(conn, project) or f"[holo2] project {target.path} enabled")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    finally:
        conn.close()


def held_idle(held, pool, conn, project):
    """Let existing workers finish, then acknowledge a held scheduler's exit."""
    if not held or pool:
        return False
    print(held)
    store.record_loop_return(conn, project)
    return True


def reconcile_tick(target, conn, project, provider, first_tick):
    from holophyte.reconcile import _reconcile_pull_requests
    if not first_tick:
        _reconcile_pull_requests(target, conn, project, provider)


def disabled_startup(target, out=None):
    """Report disabled targets before probing routes or starting a supervisor."""
    if not target.store_path.exists():
        return False
    from store.read import open_readonly
    conn = open_readonly(target.store_path)
    try:
        admission, note = state(conn, target)
        if admission != "disabled":
            return False
        print(f"[holo2] project {target.path} disabled: {note}", file=out)
        return True
    finally:
        conn.close()
