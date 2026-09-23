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
    row = conn.execute("SELECT admission, holdNote FROM projects WHERE id = ?",
                       (project_of(conn, target),)).fetchone()
    return row or ("enabled", None)


def project_of(conn, target):
    """The store's project id for `target`'s checkout, or None."""
    paths = canonical_projects(conn)
    return next((key for key, path in paths.items()
                 if path == str(target.path.resolve())), None)


def set_hold(conn, target, holding, note):
    """Hold or release `target`'s project with `note`, creating its row
    from `[board]` when the store has none; the project id. ValueError
    for a project already so, or one with neither a row nor `[board]`.
    `--hold`/`--release-hold` and the daemon's routes share it (KO-609)."""
    project = project_of(conn, target)
    if project is None:
        settings = board_config(target)
        if settings is None:
            raise ValueError("project has no store row or [board] configuration")
        project = store.ensure_project(conn, settings.team, target.path)
    (store.hold if holding else store.release_hold)(conn, project, note)
    return project


def change(target, holding, note):
    conn = open_store(target)
    try:
        project = set_hold(conn, target, holding, note)
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
    # Not the daemon's boundary: a newer store is the sweep's open to judge.
    conn = open_readonly(target.store_path, daemon_boundary=False)
    try:
        admission, note = state(conn, target)
        if admission != "disabled":
            return False
        print(f"[holo2] project {target.path} disabled: {note}", file=out)
        return True
    finally:
        conn.close()
