"""A criterion witnessed once by a test in an ignored worktree folder that
no commit holds."""
import posixpath
import re

DIRECTORY = ".holophyte-witness"
TAIL = re.compile(r"\(witnessed once:\s*`([^`\n]+)`\s*\.?\)\.?\s*$")


def is_once(path):
    return posixpath.dirname(posixpath.normpath(path)) == DIRECTORY


def tail_paths(texts):
    return [(match.group(1).strip() if (match := TAIL.search(text.strip()))
             else None) for text in texts]


def _listed(criteria):
    pairs = [item if isinstance(item, (tuple, list)) else (n, item)
             for n, item in enumerate(criteria or (), 1)]
    return "".join(f"C{n}: `{path}`\n" for (n, _), path in
                   zip(pairs, tail_paths([text for _, text in pairs]))
                   if path and is_once(path))


def implementer_brief(criteria):
    listed = _listed(criteria)
    if not listed:
        return ""
    return ("\n\nThese criteria are witnessed once, by a test kept out of "
            "every commit:\n" + listed
            + f"The factory ignores `{DIRECTORY}/`: write each file there and "
            "leave it uncommitted; never stage it with `git add -f`. The "
            "verify line that names the file runs it before each review.")


def reviewer_brief(criteria):
    listed = _listed(criteria)
    if not listed:
        return ""
    return ("These criteria are witnessed once:\n" + listed
            + "Each file is in the worktree but in no commit. Read it and "
            "run it there by the verify line that names it, and cite it as "
            "`PATH::Class::test`.\n\n")
