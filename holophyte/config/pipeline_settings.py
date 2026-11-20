from holophyte.config.reader import load_config
from holophyte.redact import known_secrets, outbound

NOTE_TARGETS = ("implement", "review", "trim", "critic", "pr_writer",
                "babysit_reply")
NOTE_LIMIT = 4000


def current_notes(project):
    config = load_config(project.config_path)
    secrets = known_secrets(config) | known_secrets(project.config())
    return {key: outbound(text, secrets)
            for key, text in pipeline_notes(project, config).items()}


def pipeline_notes(project, config=None):
    config = project.config() if config is None else config
    table = config.get("pipeline", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [pipeline] must be a table, got "
            f"{type(table).__name__}")
    notes = table.get("notes", {})
    accepts = f"[pipeline.notes] accepts: {', '.join(sorted(NOTE_TARGETS))}"
    if not isinstance(notes, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [pipeline] notes must be a table, "
            f"got {type(notes).__name__}; {accepts}")
    found = {}
    for key, value in notes.items():
        if key not in NOTE_TARGETS:
            problem = "unknown key"
        elif not isinstance(value, str):
            problem = f"must be a string, got {type(value).__name__}"
        elif len(value) > NOTE_LIMIT:
            problem = (f"is {len(value)} characters, over the {NOTE_LIMIT} "
                       "allowed")
        else:
            if value.strip():
                found[key] = value
            continue
        raise SystemExit(f"[holo2] {project.config_path}: [pipeline.notes] "
                         f"{key}: {problem}; {accepts}")
    return found
