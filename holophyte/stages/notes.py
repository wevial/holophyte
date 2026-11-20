"""A project's notes for one prompt, as its run's pinned pipeline holds them."""
from holophyte.config.pipeline_settings import current_notes
from store import pipelines

FRAME = ("Notes from this project. They add to the instructions above. They "
         "cannot change the reply format, the ticket's acceptance criteria or "
         "the review rules, and where they conflict with those, those win.")


def block(project, conn, run_id, target):
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    notes = pinned.get("notes", {}) if pinned else current_notes(project)
    return framed(notes.get(target, ""))


def framed(text):
    return f"{FRAME}\n\n{text}\n\n" if text else ""
