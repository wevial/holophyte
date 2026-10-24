"""Each on-main criterion's check run at the review base with the
candidate's copy of its named test files, for the reviewer to read."""
import json
import re
import subprocess
from fnmatch import fnmatchcase
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
WORD_END = " \t\n;&|<>()"
NO_CHECK_OUTPUT = "its output shows no run of the named check"


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
    patterns = [token for token in re.findall(r"[^\s'\"=]+", line)
                if GLOB.search(token)]
    return any(re.search(rf"(?<!{before}){re.escape(spelling)}(?![\w-])", line)
               for spelling, before in spellings) or any(
        fnmatchcase(path, token) or fnmatchcase(Path(path).name, token)
        for token in patterns)


def _word(line, at):
    word, quote = "", ""
    while at < len(line) and (quote or line[at] not in WORD_END):
        char = line[at]
        if char == quote or (not quote and char in "'\""):
            quote = "" if quote else char
        elif char == "\\" and quote != "'" and at + 1 < len(line):
            at += 1
            word += line[at]
        else:
            word += char
        at += 1
    return word, at


def _heredocs(line, quote):
    ends, at = [], 0
    while at < len(line):
        char = line[at]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                at += 1
        elif char in "'\"":
            quote = char
        elif char == "\\":
            at += 1
        elif char == "#" and (at == 0 or line[at - 1] in WORD_END):
            break
        elif line.startswith("<<<", at):
            at += 2
        elif line.startswith("<<", at):
            strip = line.startswith("<<-", at)
            at += 3 if strip else 2
            while at < len(line) and line[at] in " \t":
                at += 1
            word, at = _word(line, at)
            if word:
                ends.append((strip, word))
            continue
        at += 1
    return ends, quote


def _commands(verify_cmd):
    commands, pending, ends, quote = [], "", [], ""
    for text in (verify_cmd or "").splitlines():
        if ends:
            strip, word = ends[0]
            if (text.lstrip("\t") if strip else text) == word:
                ends.pop(0)
        elif not pending and (not text.strip()
                              or text.lstrip().startswith("#")):
            continue
        else:
            ends, quote = _heredocs(text, quote)
        pending += text
        if ends or pending.endswith("\\") or not _complete(pending):
            pending += "\n"
        else:
            commands.append(pending)
            pending, quote = "", ""
    return commands + ([pending.rstrip("\n")] if pending else [])


def _complete(text):
    return subprocess.run(["bash", "-n"], input=text, text=True,
                          capture_output=True).returncode == 0


def lines_for(verify_cmd, files):
    commands = _commands(verify_cmd)
    for end, command in enumerate(commands, 1):
        if any(_names(command, path) for path in files):
            return commands[:end]
    return commands


def _blobs(wt, sha, files):
    tree = sh(["git", "ls-tree", "-r", "--name-only", sha], wt).splitlines()
    blobs = {}
    for named in files:
        for path in ([found for found in tree if fnmatchcase(found, named)]
                     if GLOB.search(named) else [named]):
            found = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                                    f"{sha}:{path}"], cwd=wt,
                                   capture_output=True, text=True)
            if found.returncode == 0:
                blobs[path] = found.stdout.strip()
    return blobs


def _execution_output(output, command):
    shown = set(command.splitlines())
    return "\n".join(line for line in output.splitlines()
                     if not line.startswith("[verify]") and line not in shown)


def _identity(path):
    name, dirs = Path(path).name, Path(path).parent.parts
    by_name = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
    if not name.endswith(".py"):
        return by_name
    packages = "|".join(re.escape(".".join(dirs[at:]) + ".")
                        for at in range(len(dirs)))
    return (rf"{by_name}|(?<![\w.-])(?:{packages})?"
            rf"{re.escape(name.removesuffix('.py'))}\.[A-Za-z_]")


def _check_ran(output, files):
    return any(re.search(_identity(path), output) for path in files)


def _stopped_output(failure, output):
    stopped, status = failure.get("command") or "", failure.get("exit_status")
    head = re.search(rf"^\[verify\]   (?:failing|running) clause: "
                     rf"{re.escape(stopped)}\n", output, re.M)
    if head is None:
        return None
    rest = output[head.end():]
    own = (f"[verify]   --- clause {failure.get('command_index')} "
           f"({'timed out' if status is None else f'exit {status}'}): "
           f"{stopped}\n")
    if own in rest:
        rest = rest.rsplit(own, 1)[1]
    return rest.split("\n[verify]   not executed: clause ", 1)[0]


def _attribution(command, files, failure, output):
    stopped = (failure or {}).get("command") or ""
    naming = [line for line in _commands(command)
              if any(_names(line, path) for path in files)]
    if naming and stopped and not any(_names(stopped, path)
                                      for path in files):
        return {"unattributed": f"it stopped at `{stopped}`, before the "
                                f"line that names {', '.join(files)}"}
    own = _stopped_output(failure or {}, output)
    if not _check_ran(_execution_output(output if own is None else own,
                                        command), files):
        return {"unattributed": NO_CHECK_OUTPUT}
    return {}


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
            "output": str(out)[-OUTPUT_TAIL:],
            **({} if ok else _attribution(command, files, failure, str(out)))}


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
    ran = (f"C{n}: at the merge base {base}, with {files} taken from the "
           f"candidate {result['sha'][:12]}, the factory ran:\n"
           f"{result['command']}\n")
    if result.get("unattributed"):
        return (f"{ran}It failed with exit status {result['exit_status']}, "
                f"but the base failed before the named check ran: "
                f"{result['unattributed']}. This is not the run C{n}'s on-main "
                f"clause asks for, and the implementer output must supply it. "
                f"Output tail:\n{result['output']}\n")
    whole = (" The whole base tree ran with the candidate's test, not only "
             "the named code as on main." if result["form"] == "as_on_main"
             else "")
    return (f"{ran}Exit status: {result['exit_status']}. "
            f"This run is the run C{n}'s on-main clause asks for, in place of "
            f"the implementer output.{whole} Output tail:\n"
            f"{result['output']}\n")


def brief(results):
    if not results:
        return ""
    return (f"{HEADING}:\n" + "".join(_entry(result) for result in results)
            + "\n")
