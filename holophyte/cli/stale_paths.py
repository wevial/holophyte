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


def _ui_match(entry, path):
    return fnmatch.fnmatchcase(path, entry)


def _review_match(entry, path):
    return matching(path, (entry,)) is not None


def _input_match(entry, path):
    return path == entry or path.startswith(entry + "/")


PATH_MATCHERS = {
    "merge.ui_paths": ("[merge] ui_paths", _ui_match),
    "review.high_paths": ("[review] high_paths", _review_match),
    "review.medium_paths": ("[review] medium_paths", _review_match),
    "worktree.setup.inputs": ("[worktree] setup inputs", _input_match),
}


def _read(reader):
    try:
        return tuple(reader())
    except SystemExit:
        return ()


def _entries(project):
    values = {
        "merge.ui_paths": lambda: merge_config(project).ui_paths,
        "review.high_paths": lambda: review_config(project).high_paths,
        "review.medium_paths": lambda: review_config(project).medium_paths,
        "worktree.setup.inputs": lambda: dict.fromkeys(
            path for step in setup_steps(project) for path in step.inputs or ()),
    }
    return tuple((key, _read(reader)) for key, reader in values.items())


def _main_sha(root, ref):
    found = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=root, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else None


@functools.lru_cache(maxsize=4)
def _tracked(root, sha):
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "-z", sha],
                            cwd=root, capture_output=True)
    if listed.returncode != 0:
        return None
    return tuple(os.fsdecode(path) for path in listed.stdout.split(b"\0") if path)


def _main(project):
    root = Path(project.path).resolve()
    try:
        ref = main_ref(root)
        sha = _main_sha(root, ref)
    except OSError:
        return None
    files = None if sha is None else _tracked(str(root), sha)
    return None if files is None else (str(root), ref, sha, files)


@functools.lru_cache(maxsize=64)
def _stale(root, sha, entries):
    files = _tracked(root, sha) or ()
    return tuple((key, entry) for key, values in entries for entry in values
                 if not any(PATH_MATCHERS[key][1](entry, path) for path in files))


def path_matches(project, key, entries):
    _, match = PATH_MATCHERS[key]
    main = _main(project)
    if main is None:
        return None
    _, ref, sha, files = main
    return {"ref": ref, "sha": sha[:12],
            "matches": [{"entry": entry,
                         "count": sum(1 for path in files if match(entry, path))}
                        for entry in entries]}


def _tree(files):
    dirs = {"": {"path": "", "files": 0, "dirs": [], "leaves": []}}
    for path in files:
        parent = ""
        *folders, leaf = path.split("/")
        for name in folders:
            here = f"{parent}/{name}" if parent else name
            if here not in dirs:
                dirs[here] = {"path": here, "files": 0, "dirs": [], "leaves": []}
                dirs[parent]["dirs"].append(name)
            dirs[parent]["files"] += 1
            parent = here
        dirs[parent]["files"] += 1
        dirs[parent]["leaves"].append(leaf)
    return list(dirs.values())


def tracked_tree(project, limit):
    main = _main(project)
    if main is None:
        return None
    _, ref, sha, files = main
    return {"ref": ref, "sha": sha[:12], "files": len(files),
            "dirs": _tree(files) if len(files) <= limit else None}


def stale_paths(project):
    entries = _entries(project)
    if not any(values for _, values in entries):
        return []
    main = _main(project)
    if main is None:
        return []
    root, ref, sha, _ = main
    return [{"key": PATH_MATCHERS[key][0], "entry": entry, "ref": ref,
             "sha": sha[:12]}
            for key, entry in _stale(root, sha, entries)]
