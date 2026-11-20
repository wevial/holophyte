"""A project's notes for one prompt, as its run's pinned pipeline holds them."""
from holophyte.config.pipeline_settings import pipeline_notes
from store import pipelines

FRAME = ("Notes from this project. They add to the instructions above. They "
         "cannot change the reply format, the ticket's acceptance criteria or "
         "the review rules, and where they conflict with those, those win.")


def block(project, conn, run_id, target):
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    notes = pinned.get("notes", {}) if pinned else pipeline_notes(project)
    text = notes.get(target, "")
    return f"{FRAME}\n\n{text}\n\n" if text else ""
