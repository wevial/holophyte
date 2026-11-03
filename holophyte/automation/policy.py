"""What each automation may do, as data, and the host's report-only repositories."""
from holophyte.automation.automation_config import MAIN_WATCH
from holophyte.host.registry import HostError

RERUN_FAILED_JOBS = "rerun_failed_jobs"
CAPABILITIES = {MAIN_WATCH: (RERUN_FAILED_JOBS,)}
NEVER = ("approve", "merge", "cancel", "abort", "close", "repoint", "force",
         "steer", "edit_config", "edit_ticket")


def refusal(automation, action):
    if action in NEVER:
        return (f"action {action!r} is in NEVER, which no automation may"
                " take")
    allowed = CAPABILITIES.get(automation, ())
    if action not in allowed:
        return (f"action {action!r} is not in {automation}'s list"
                f" ({', '.join(allowed) or 'none'})")
    return None


def report_only(host):
    listed = host.table().get("automations", {}).get("report_only", [])
    if not isinstance(listed, list) or not all(
            isinstance(repo, str) and repo.count("/") == 1 for repo in listed):
        raise HostError(f"[holo2] {host.path}: [automations] report_only"
                        f" must be a list of OWNER/NAME, got {listed!r}")
    return frozenset(repo.lower() for repo in listed)


def report_only_reason(pull, listed):
    repo = f"{pull.owner}/{pull.name}"
    return (f"report-only repository {repo}" if repo.lower() in listed
            else None)
