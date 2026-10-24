"""Each on-main criterion's check run at the review base with the
candidate's copy of its named test files, for the reviewer to read."""
import json
import re
import subprocess
from pathlib import Path

import store
from holophyte.agents.review_workspace import review_base
from holophyte.babysit.main_checkout import detached_main
from holophyte.config.config_tables import verify_config
from holophyte.loop.gates import run_verify, sh
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import safe_print as print
from holophyte.review import criterion_ids
from holophyte.review.witness_precheck import GLOB, named_files

EVENT = "main_baseline"
OUTPUT_CLAUSE = "that run's output is in the implementer output"
OUTPUT_TAIL = 2000
HEADING = "Baseline at the merge base"
SENTENCE = re.compile(r"(?<=[.!?])\s+")
VERIFY_FAILED = "verify failed at the candidate"


def _form(sentence):
    if OUTPUT_CLAUSE not in sentence:
        return None
    if sentence.startswith("On main"):
        return "on_main"
    if "as on main" in sentence:
        return "as_on_main"
    if sentence.startswith("With the change reverted"):
        return "reverted"
    return "other"


def demands(text):
    """The forms of each sentence of `text` that asks for a run's output."""
    return [form for sentence in SENTENCE.split(text.strip())
            if (form := _form(sentence.strip()))]


def wanted(pairs):
    """(criterion, form, named files) for each criterion the factory runs."""
    named = named_files([text for _, text in pairs])
    found = []
    for (n, text), files in zip(pairs, named):
        form = next((form for form in demands(text) if form != "other"), None)
        if form and files:
            found.append((n, form, files))
    return found


def _names(line, path):
    stem = Path(path).name.rsplit(".", 1)[0]
    spellings = ((path, r"[\w-]"),
                 (path.removesuffix(".py").replace("/", "."), r"[\w-]"),
                 (stem, r"[\w/.-]"))
    return any(re.search(rf"(?<!{before}){re.escape(spelling)}(?![\w-])", line)
               for spelling, before in spellings)


def lines_for(verify_cmd, files):
    lines = [text for text in (verify_cmd or "").splitlines()
             if text.strip() and not text.lstrip().startswith("#")]
    for end, line in enumerate(lines, 1):
        if any(_names(line, path) for path in files):
            return lines[:end]
    return lines


def _blobs(wt, sha, files):
    blobs = {}
    for path in files:
        if GLOB.search(path):
            continue
        found = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                                f"{sha}:{path}"], cwd=wt, capture_output=True,
                               text=True)
        if found.returncode == 0:
            blobs[path] = found.stdout.strip()
    return blobs


def _recorded(conn, run_id, key):
    for (payload,) in conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
            " ORDER BY seq DESC", (run_id, EVENT)).fetchall():
        result = json.loads(payload)
        if result.get("ran") and all(result.get(name) == value
                                     for name, value in key.items()):
            return result
    return None


def _run(project, conn, run_id, beat_s, wt, base, sha, files, command):
    with detached_main(project, conn, run_id, beat_s, wt, base) as (
            tree, setup_failure):
        if setup_failure is not None:
            return {"ran": False,
                    "reason": f"its worktree setup failed:\n{setup_failure}"}
        sh(["git", "checkout", sha, "--", *files], tree)
        with heartbeat_while(conn, run_id, beat_s):
            ok, out = run_verify(command, tree,
                                 timeout=verify_config(project).timeout_sec,
                                 conn=conn, run_id=run_id, project=project,
                                 rerun=False)
    failure = getattr(out, "failure", None)
    status = 0 if ok else (failure or {}).get("exit_status")
    return {"ran": True, "passed": ok, "exit_status": status,
            "output": str(out)[-OUTPUT_TAIL:]}


def _baseline(project, conn, run_id, beat_s, wt, base, sha, verify_cmd, want):
    n, form, named = want
    blobs = _blobs(wt, sha, named)
    command = "\n".join(lines_for(verify_cmd, list(blobs)))
    key = {"criterion": n, "base": base, "files": list(blobs), "blobs": blobs,
           "command": command}
    result = dict(key, form=form, sha=sha)
    if not blobs:
        return dict(result, ran=False, reason=f"none of {', '.join(named)} "
                    f"exists at the candidate {sha[:12]}")
    reused = _recorded(conn, run_id, key)
    if reused is not None:
        return dict(reused, form=form, sha=sha)
    try:
        result.update(_run(project, conn, run_id, beat_s, wt, base, sha,
                           list(blobs), command))
    except RuntimeError as exc:
        result.update(ran=False, reason=f"the detached tree failed: {exc}")
    status = (f"exit {result['exit_status']}" if result["ran"]
              else "did not run")
    summary = f"main baseline for C{n} at {base[:12]}: {status}"
    print(f"[holo2] {summary}")
    store.record_event(conn, run_id, EVENT, summary, level="detail",
                       payload=json.dumps(result))
    return result


def baselines(project, conn, run_id, beat_s, wt, frozen, sha, criteria,
              verify_cmd, ok):
    """Per on-main criterion its baseline result, reused when the base, the
    command and the named files' blobs match a recorded run."""
    if conn is None or run_id is None:
        return []
    found = wanted(criterion_ids.numbered(conn, run_id, criteria))
    if not found:
        return []
    base = review_base(wt, frozen, sha)
    if not ok:
        return [{"criterion": n, "base": base, "files": files, "sha": sha,
                 "form": form, "ran": False, "reason": VERIFY_FAILED}
                for n, form, files in found]
    return [_baseline(project, conn, run_id, beat_s, wt, base, sha, verify_cmd,
                      want) for want in found]


def gap(result):
    if not result.get("passed"):
        return None
    return (f"criterion C{result['criterion']}'s check passes at the merge "
            f"base {result['base'][:12]}; its on-main clause says it fails")


def _entry(result):
    n, base = result["criterion"], result["base"][:12]
    files = ", ".join(result["files"])
    if not result["ran"]:
        return (f"C{n}: the baseline at {base} did not run: "
                f"{result['reason']}\n")
    whole = (" The whole base tree ran with the candidate's test, not only "
             "the named code as on main." if result["form"] == "as_on_main"
             else "")
    return (f"C{n}: at the merge base {base}, with {files} taken from the "
            f"candidate {result['sha'][:12]}, the factory ran:\n"
            f"{result['command']}\nExit status: {result['exit_status']}. "
            f"This run is the run C{n}'s on-main clause asks for, in place of "
            f"the implementer output.{whole} Output tail:\n"
            f"{result['output']}\n")


def brief(results):
    if not results:
        return ""
    return (f"{HEADING}:\n" + "".join(_entry(result) for result in results)
            + "\n")
