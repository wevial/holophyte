"""The transcript read allow-list; absent means no filesystem access."""
from pathlib import Path


def transcript_roots(target, value):
    """Resolve roots like token_file: relative to config, with home expansion."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(
            not isinstance(root, str) or not root.strip() for root in value):
        raise SystemExit(f'[holo2] {target.config_path}: [serve] transcripts '
                         'must be a path or a list of non-empty paths')
    paths = []
    for root in value:
        path = Path(root).expanduser()
        if not path.is_absolute():
            path = target.config_path.parent / path
        paths.append(path.resolve())
    return tuple(paths)
