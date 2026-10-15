from pathlib import Path
from time import monotonic

from holophyte.redact import outbound

PROGRESS_EVERY_S = 60
DESCRIPTION_CHARS = 80
CONTAINER_MOUNT = "/workspace/"
EDITING = ("Edit", "MultiEdit", "Write", "NotebookEdit")


class TurnProgress:

    def __init__(self, conn, run_id, role, worktree, secrets):
        self.conn, self.run_id, self.role = conn, run_id, role
        self.secrets = secrets
        self.roots = (CONTAINER_MOUNT, f"{Path(worktree)}/",
                      f"{Path(worktree).resolve()}/")
        self.calls = self.recorded_calls = 0
        self.recorded_at = None
        self.label = None

    def seen(self, document):
        if document.get("type") != "assistant":
            return
        message = document.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        subagent = document.get("parent_tool_use_id") is not None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self.calls += 1
                label = self.labelled(block)
                self.label = f"subagent {label}" if subagent else label
        if self.calls == self.recorded_calls:
            return
        now = monotonic()
        if (self.recorded_at is not None
                and now - self.recorded_at < PROGRESS_EVERY_S):
            return
        self.recorded_at, self.recorded_calls = now, self.calls
        self.record()

    def record(self):
        import store
        plural = "" if self.calls == 1 else "s"
        summary = (f"{self.role} turn: {self.calls} tool call{plural};"
                   f" last: {self.label}")
        store.record_event(self.conn, self.run_id, "turn_progress",
                           outbound(summary, self.secrets))

    def labelled(self, block):
        name = block.get("name")
        given = block.get("input")
        given = given if isinstance(given, dict) else {}
        description = given.get("description")
        description = (outbound(description, self.secrets)[:DESCRIPTION_CHARS]
                       .strip() if isinstance(description, str) else "")
        if name in EDITING or name == "Read":
            path = given.get("file_path") or given.get("notebook_path")
            verb = "reading" if name == "Read" else "editing"
            return f"{verb} {self.relative(path)}" if isinstance(path, str) else verb
        if name == "Bash":
            return f"running {description}" if description else "running a command"
        if name in ("Grep", "Glob"):
            return "searching"
        if name in ("Agent", "Task"):
            return f"subagent: {description}" if description else "subagent"
        return name if isinstance(name, str) else "a tool"

    def relative(self, path):
        for root in self.roots:
            if path.startswith(root):
                return path[len(root):]
        return path
