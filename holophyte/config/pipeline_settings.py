NOTE_TARGETS = ("implement", "review", "trim", "critic", "pr_writer",
                "babysit_reply")
NOTE_LIMIT = 4000


def pipeline_notes(project):
    table = project.config().get("pipeline", {})
    notes = table.get("notes", {}) if isinstance(table, dict) else {}
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
                found[key] = value.strip()
            continue
        raise SystemExit(f"[holo2] {project.config_path}: [pipeline.notes] "
                         f"{key}: {problem}; {accepts}")
    return found
