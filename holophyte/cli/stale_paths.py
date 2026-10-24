"""Config path entries that match no file tracked on the project's main."""
import fnmatch
import functools
import os
import subprocess
from pathlib import Path

from holophyte.config.config_tables import merge_config
from holophyte.config.review_settings import review_config
from holophyte.config.worktree_settings import setup_steps
from holophyte.review.blast_radius import matching
from holophyte.review.briefs import main_ref

UI_PATHS = "[merge] ui_paths"
HIGH_PATHS = "[review] high_paths"
MEDIUM_PATHS = "[review] medium_paths"
SETUP_INPUTS = "[worktree] setup inputs"


def _ui_matches(entry, files):
    return any(fnmatch.fnmatchcase(path, entry) for path in files)


def _review_matches(entry, files):
    return any(matching(path, (entry,)) for path in files)


def _input_matches(entry, files):
    return any(path == entry or path.startswith(entry + "/") for path in files)


MATCHERS = {UI_PATHS: _ui_matches, HIGH_PATHS: _review_matches,
            MEDIUM_PATHS: _review_matches, SETUP_INPUTS: _input_matches}


def _read(reader):
    try:
        return tuple(reader())
    except SystemExit:
        return ()


def _entries(project):
    return (
        (UI_PATHS, _read(lambda: merge_config(project).ui_paths)),
        (HIGH_PATHS, _read(lambda: review_config(project).high_paths)),
        (MEDIUM_PATHS, _read(lambda: review_config(project).medium_paths)),
        (SETUP_INPUTS, _read(lambda: dict.fromkeys(
            path for step in setup_steps(project) for path in step.inputs or ()))),
    )


def _main_sha(root, ref):
    found = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=root, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else None


@functools.lru_cache(maxsize=64)
def _stale(root, ref, sha, entries):
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "-z", sha],
                            cwd=root, capture_output=True)
    if listed.returncode != 0:
        return ()
    files = [os.fsdecode(path) for path in listed.stdout.split(b"\0") if path]
    return tuple((key, entry) for key, values in entries for entry in values
                 if not MATCHERS[key](entry, files))


def stale_paths(project):
    root = Path(project.path).resolve()
    entries = _entries(project)
    if not root.is_dir() or not any(values for _, values in entries):
        return []
    try:
        ref = main_ref(root)
        sha = _main_sha(root, ref)
    except OSError:
        return []
    if sha is None:
        return []
    return [{"key": key, "entry": entry, "ref": ref, "sha": sha[:12]}
            for key, entry in _stale(str(root), ref, sha, entries)]
