import re
import subprocess
from functools import cached_property
from pathlib import Path

import ticket_template

FUNCTION_SPAN_RE = re.compile(r"((?:[A-Za-z_]\w*\.)*)([A-Za-z_]\w*)\(\)")
# At least one lowercase letter, so a constant like `MAX_RUNS` is no class.
CLASS_SPAN_RE = re.compile(r"[A-Z][A-Z0-9]*[a-z][A-Za-z0-9]*")
PLACEHOLDER_RE = re.compile(r"[A-Z][A-Z0-9_]*")
PAIR_GAPS = frozenset({"in", "from", "of", "("})
SPAN_RE = re.compile(r"`([^`\n]+)`")


class MainTree:
    def __init__(self, repo):
        self.repo = repo
        self._texts = {}
        self._holders = {}

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c",
                               "core.quotePath=false", *args],
                              capture_output=True, text=True,
                              errors="replace")

    @cached_property
    def files(self):
        listed = self._git("ls-tree", "-r", "--name-only", "main")
        return set(listed.stdout.splitlines() if listed.returncode == 0
                   else ())

    @cached_property
    def trees(self):
        return {str(p) for f in self.files for p in Path(f).parents
                if str(p) != "."}

    @cached_property
    def basenames(self):
        return {Path(f).name for f in self.files}

    @cached_property
    def stems(self):
        return {Path(f).stem for f in self.files}

    def has(self, path):
        return path in self.files or path in self.trees

    def text(self, path):
        if path not in self._texts:
            blob = self._git("cat-file", "blob", f"main:{path}")
            self._texts[path] = blob.stdout if blob.returncode == 0 else None
        return self._texts[path]

    def holders(self, name):
        if name not in self._holders:
            found = self._git("grep", "-l", "-w", "-e", name, "main", "--")
            self._holders[name] = sorted(
                line.removeprefix("main:") for line in found.stdout.splitlines()
                if found.returncode == 0)
        return self._holders[name]


def declared_new(path, declarations):
    files, directories = declarations
    normalized = str(Path(path))
    return (normalized in files
            or any(normalized.startswith(d + "/") for d in directories))


def placeholder(path):
    return any(PLACEHOLDER_RE.fullmatch(segment.split(".")[0])
               for segment in path.split("/"))


def path_shaped_word(tree, path):
    parent = str(Path(path).parent)
    return ("/" in path and not path.endswith("/") and not Path(path).suffix
            and parent not in tree.trees)


def path_findings(tree, named):
    stale, advisories = [], []
    for label, path in named:
        normalized = str(Path(path))
        if tree.has(normalized):
            continue
        if "/" in normalized:
            stale.append(f"`{path}` (named in {label}) is not on main")
        elif normalized not in tree.basenames:
            advisories.append(f"`{path}` (named in {label}) matches no"
                              " tracked file on main")
    return stale, advisories


def _paired_paths(item):
    masked = ticket_template._mask_code_spans(item)
    paths = {span.start(): path
             for span, path in ticket_template._prose_paths(item)}
    spans = list(SPAN_RE.finditer(item))
    first = spans[0] if spans else None
    lead = (paths.get(first.start()) if first is not None
            and re.fullmatch(r"[-*+]?\s*", item[:first.start()].strip())
            and masked[first.end():].lstrip().startswith(":") else None)
    pairs = {}
    for span, after in zip(spans, spans[1:] + [None]):
        gap = after and masked[span.end():after.start()].strip()
        if after is not None and gap in PAIR_GAPS and after.start() in paths:
            pairs[span.start()] = paths[after.start()]
        elif lead is not None and span is not first:
            pairs[span.start()] = lead
    return pairs


def _named_symbols(tree, item):
    masked = ticket_template._mask_code_spans(item)
    for span in SPAN_RE.finditer(item):
        text = span.group(1)
        function = FUNCTION_SPAN_RE.fullmatch(text)
        if function:
            name = function.group(2)
            receiver = function.group(1).rstrip(".").rpartition(".")[2]
            if receiver[:1].islower() and receiver not in tree.stems:
                continue
        elif CLASS_SPAN_RE.fullmatch(text):
            name = text
        else:
            continue
        sentence = ticket_template._sentence_before(masked, span.start())
        if not re.search(r"\bnew\b", sentence, re.I):
            yield span.start(), text, name


def _item_paths(tree, item, declarations):
    present = []
    for _, path in ticket_template._prose_paths(item):
        normalized = str(Path(path))
        if (normalized not in present and not declared_new(path, declarations)
                and tree.text(normalized) is not None):
            present.append(normalized)
    return present


def _symbol_finding(tree, text, name, label, paired, present):
    if paired is None:
        if tree.holders(name):
            return None, None
        files = ", ".join(f"`{p}`" for p in present)
        return f"`{text}` (named in {label}) is not in {files} on main", None
    if re.search(rf"\b{re.escape(name)}\b", tree.text(paired)):
        return None, None
    holders = tree.holders(name)
    missing = f"`{text}` (named in {label}) is not in `{paired}` on main"
    if not holders:
        return missing, None
    return None, missing + " but is in " + ", ".join(f"`{h}`" for h in holders)


def symbol_findings(tree, t, declarations):
    stale, advisories = [], []
    notes = t.sections.get("Implementation notes", "")
    for i, item in enumerate(ticket_template._list_item_blocks(notes), 1):
        present = _item_paths(tree, item, declarations)
        if not present:
            continue
        pairs = _paired_paths(item)
        for start, text, name in _named_symbols(tree, item):
            paired = pairs.get(start)
            if paired is not None:
                paired = str(Path(paired))
                if not tree.has(paired) and (
                        "/" in paired or declared_new(paired, declarations)):
                    continue
                if tree.text(paired) is None:
                    paired = None
            reason, advisory = _symbol_finding(
                tree, text, name, f"Implementation notes #{i}", paired,
                present)
            stale += [reason] if reason else []
            advisories += [advisory] if advisory else []
    return stale, advisories
