"""Canonical project identities without guessing legacy working directories."""
from pathlib import Path


def canonical_projects(conn):
    """Read canonical paths, refusing ambiguous rows before identity decisions."""
    paths = {}
    for project, stored in conn.execute(
            "SELECT id, repoPath FROM projects ORDER BY id"):
        path = Path(stored)
        if not path.is_absolute():
            raise ValueError(
                f"project {project} has relative repoPath {stored!r}; operator repair "
                "required: verify its original base and set a canonical absolute path")
        paths[project] = str(path.resolve())
    return paths
