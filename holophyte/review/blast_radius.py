"""A review round's blast-radius tier: set from the diff, raised, never lowered."""
import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass

import store
import ticket_template
from holophyte.config.review_settings import review_config
from holophyte.review.briefs import _changed_files

BASE_HIGH_PATHS = (
    ".github/workflows/*", ".github/actions/*", ".gitlab-ci.yml", ".circleci/*",
    "Jenkinsfile",
    "Dockerfile", "*.Dockerfile", "docker-compose*.yml", "deploy/*", "*/deploy/*",
    "wrangler.toml", "wrangler.json", "wrangler.jsonc", "vercel.json",
    "netlify.toml", "fly.toml", "*.tf", "CODEOWNERS", ".github/dependabot.yml",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "bun.lockb", "pyproject.toml", "requirements*.txt",
    "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock", "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    "migrations/*", "*/migrations/*", "migrate/*", "*/migrate/*", "alembic/*",
    "*/alembic/*",
)
TIERS = ("low", "medium", "high")
RAISING_FIELD = ("high", "medium")
UNCOUNTED_PREFIXES = ("tests/", "test/", "docs/")
DECLARED_RE = re.compile(
    r"^[\s>*`_-]*BLAST RADIUS:[*`_\s]*(high|medium|low)\b[\s*`_]*[—–:-]*\s*"
    r"(.*?)[\s*`_]*$", re.IGNORECASE | re.MULTILINE)
IMPORT_RE = re.compile(r"^\s*import\s+(.+)$")
FROM_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$")
IMPORT_LINE = r"^[[:space:]]*(import|from)[[:space:]]"
BRIEF = ("\n\nInclude in your reply one line naming the riskiest thing the "
         "change touches:\nBLAST RADIUS: high|medium|low — reason")


@dataclass(frozen=True)
class Assessment:
    tier: str
    reasons: list
    gated: list


def matching(path, patterns):
    name = path.rsplit("/", 1)[-1]
    return next((pattern for pattern in patterns
                 if fnmatch.fnmatchcase(path, pattern)
                 or ("/" not in pattern and fnmatch.fnmatchcase(name, pattern))),
                None)


def module_name(path):
    parts = path.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _names(text):
    text = text.split("#", 1)[0].replace("(", " ").replace(")", " ")
    return {part.split()[0] for part in text.replace("\\", " ").split(",")
            if part.split()}


def imported(line):
    found = FROM_RE.match(line)
    if found:
        package = found.group(1)
        return {package} | {f"{package}.{name}" for name in _names(found.group(2))}
    found = IMPORT_RE.match(line)
    return _names(found.group(1)) if found else set()


def importer_counts(root, sha, modules):
    run = subprocess.run(
        ["git", "grep", "-z", "-I", "-E", "-e", IMPORT_LINE, sha, "--", "*.py"],
        cwd=root, capture_output=True, text=True, errors="replace")
    if run.returncode not in (0, 1):
        raise subprocess.CalledProcessError(run.returncode, run.args,
                                            run.stdout, run.stderr)
    importers = {path: set() for path in modules.values()}
    for record in run.stdout.split("\n"):
        name, _, line = record.partition("\0")
        source = name.partition(":")[2]
        for module in imported(line) & modules.keys():
            if modules[module] != source:
                importers[modules[module]].add(source)
    return {path: len(files) for path, files in importers.items()}


def segments(paths):
    return sorted({path.split("/", 1)[0] if "/" in path else "." for path in paths
                   if not path.startswith(UNCOUNTED_PREFIXES)
                   and not path.endswith(".md")})


def _medium_reasons(root, sha, paths, config):
    reasons = [f"medium path: {path} matches {pattern}" for path in paths
               if (pattern := matching(path, config.medium_paths))]
    modules = {module_name(path): path for path in paths if path.endswith(".py")}
    if config.fan_in and modules:
        for path, count in importer_counts(root, sha, modules).items():
            if count >= config.fan_in:
                reasons.append(f"fan-in: {path} has {count} importers "
                               f"(threshold {config.fan_in})")
    spanned = segments(paths)
    if config.packages and len(spanned) >= config.packages:
        reasons.append(f"packages: the change spans {len(spanned)} top-level "
                       f"paths ({', '.join(spanned)})")
    return reasons


def diff_tier(root, base, sha, config):
    paths = sorted(_changed_files(root, base, sha))
    reasons, gated = [], set()
    for source, patterns in (("base path", BASE_HIGH_PATHS),
                             ("project path", config.high_paths)):
        for path in paths:
            pattern = matching(path, patterns)
            if pattern:
                reasons.append(f"{source}: {path} matches {pattern}")
                gated.add(path)
    if reasons:
        return "high", reasons, sorted(gated)
    reasons = _medium_reasons(root, sha, paths, config)
    return ("medium" if reasons else "low"), reasons, []


def assess(root, base, sha, config, ticket="", declared=None, earlier=None):
    tier, reasons, gated = diff_tier(root, base, sha, config)
    raised = []
    field = (ticket_template.parse(ticket).blast_radius or "").lower()
    if field in RAISING_FIELD:
        raised.append((field, f"ticket: **Blast radius:** {field}"))
    if declared:
        raised.append((declared["tier"], f"implementer: {declared['tier']} — "
                                         f"{declared['reason']}"))
    if earlier:
        raised.append((earlier["tier"], f"earlier round: round "
                                        f"{earlier['round']} was {earlier['tier']}"))
    top = max([tier, *(level for level, _ in raised)], key=TIERS.index)
    kept = (reasons if tier == top else []) + [
        reason for level, reason in raised if level == top]
    return Assessment(top, kept, gated)


def declared(reply):
    found = DECLARED_RE.findall(str(reply or ""))
    if not found:
        return None
    tier, reason = found[-1]
    return {"tier": tier.lower(), "reason": reason}


def record_declared(conn, run_id, reply):
    found = declared(reply)
    if found is None or conn is None or run_id is None:
        return
    store.record_event(conn, run_id, "blast_radius_declared",
                       f"implementer declared blast radius {found['tier']}",
                       level="detail", payload=json.dumps(found))


def _newest(conn, run_id, kind, before=None):
    payloads = [json.loads(payload) for (payload,) in conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ? ORDER BY seq",
        (run_id, kind))]
    if before is not None:
        payloads = [payload for payload in payloads if payload["round"] < before]
    return payloads[-1] if payloads else None


def record_round(project, conn, run_id, root, base, sha, ticket, rnd):
    if conn is None or run_id is None:
        return
    found = assess(root, base, sha, review_config(project), ticket,
                   declared=_newest(conn, run_id, "blast_radius_declared"),
                   earlier=_newest(conn, run_id, "blast_radius", before=rnd))
    store.record_event(conn, run_id, "blast_radius",
                       f"round {rnd} blast radius: {found.tier}", level="detail",
                       payload=json.dumps({"round": rnd, "sha": sha,
                                           "tier": found.tier,
                                           "reasons": found.reasons,
                                           "gated": found.gated}))
