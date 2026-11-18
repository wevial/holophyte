"""Keep the docs site's Material palette on the console's Lotus theme."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONSOLE_THEME = ROOT / "console" / "src" / "app" / "theme.css"
# Each live console root showing Lotus in a scheme: its attributes and the
# operating system's colour preference.
LIVE_ROOTS = {
    "default": (({"data-theme": "light"}, "light"), ({"data-theme": "light"}, "dark"),
                ({}, "light")),
    "slate": (({"data-theme": "dark"}, "light"), ({"data-theme": "dark"}, "dark"),
              ({}, "dark")),
}
CONSOLE_MEDIA = {"width": 1440, "forced-colors": "none",
                 "prefers-reduced-motion": "no-preference"}
LOTUS_TOKENS = ("--canvas", "--side", "--ink", "--ink-2", "--ink-3",
                "--accent", "--hair")
# Each Material surface the docs paint, and the Lotus token it must carry.
SURFACES = {
    "--md-default-bg-color": "--canvas",
    "--md-primary-fg-color": "--side",
    "--md-default-fg-color": "--ink",
    "--md-typeset-color": "--ink",
    "--md-default-fg-color--light": "--ink-2",
    "--md-default-fg-color--lighter": "--ink-3",
    "--md-default-fg-color--lightest": "--hair",
    "--md-accent-fg-color": "--accent",
    "--md-typeset-a-color": "--accent",
}
# The sidebar surfaces Material shows at each viewport width in pixels: the
# drawer below 76.25em, the contents column from 60em, the navigation column
# from 76.25em.
DRAWER = (".md-nav--primary", ".md-nav--primary .md-nav",
          ".md-nav--primary .md-nav__title",
          ".md-nav--primary .md-nav__title ~ .md-nav__list")
CONTENTS = (".md-sidebar--secondary .md-sidebar__scrollwrap",
            ".md-sidebar--secondary .md-nav__title")
NAVIGATION = (".md-sidebar--primary .md-sidebar__scrollwrap",
              ".md-nav--primary > .md-nav__title")
SIDEBARS = {390: DRAWER, 1024: DRAWER + CONTENTS, 1440: NAVIGATION + CONTENTS}
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
ROOT_COMPOUND = re.compile(r"(:root|html)?((?:\[[^\]]+\]|:not\(\[[^\]]+\]\))*)")
ATTRIBUTE = re.compile(r'(:not\()?\[([\w-]+)(?:=(?:"([^"]*)"|([\w-]+)))?\]')


def blocks(css):
    """Yield (enclosing preludes, prelude, declarations) for each innermost block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"@import[^;{]*;", "", css)
    opened, start = [], 0
    for brace in re.finditer(r"[{}]", css):
        if brace.group() == "{":
            opened.append((css[start:brace.start()].strip(), brace.end(), False))
        else:
            prelude, body_start, nested = opened.pop()
            if not nested:
                yield ([outer for outer, _, _ in opened], prelude,
                       parse_declarations(css[body_start:brace.start()]))
            if opened:
                opened[-1] = opened[-1][:2] + (True,)
        start = brace.end()


def parse_declarations(body):
    found = {}
    for part in re.split(r";(?![^(]*\))", body):
        pair = re.fullmatch(r"\s*([\w-]+)\s*:\s*(.*?)\s*", part, re.DOTALL)
        if pair:
            found[pair.group(1)] = pair.group(2)
    return found


def style_rules(css):
    """Yield (media conditions, selectors, declarations) for each style rule."""
    for outer, prelude, body in blocks(css):
        if prelude.startswith("@") or not all(
                enclosing.startswith("@media") for enclosing in outer):
            unmodelled = sorted(set(body) & set(LOTUS_TOKENS))
            assert not unmodelled, f"{unmodelled} set inside {outer + [prelude]}"
            continue
        yield ([enclosing[len("@media"):].strip() for enclosing in outer],
               {selector.strip() for selector in prelude.split(",")}, body)


def length(text):
    number, unit = re.fullmatch(r"([\d.]+)(px|em)", text).groups()
    return float(number) * (16 if unit == "em" else 1)


def media_matches(condition, env):
    for part in re.split(r"\s+and\s+", condition.strip()):
        feature = re.fullmatch(r"\(\s*([\w-]+)\s*:\s*([\w.-]+)\s*\)", part)
        bound = re.fullmatch(r"\(\s*width\s*(>=|<=)\s*([\d.]+(?:px|em))\s*\)", part)
        if part in ("screen", "all"):
            continue
        if feature and feature.group(1) in ("min-width", "max-width"):
            bound_op = ">=" if feature.group(1) == "min-width" else "<="
            bound_px = length(feature.group(2))
        elif bound:
            bound_op, bound_px = bound.group(1), length(bound.group(2))
        elif feature:
            if env[feature.group(1)] != feature.group(2):
                return False
            continue
        else:
            raise AssertionError(f"unsupported media query: {condition}")
        width = env["width"]
        if not (width >= bound_px if bound_op == ">=" else width <= bound_px):
            return False
    return True


def root_specificity(selector, attributes):
    """A root compound's specificity when it matches the attributes, else None."""
    whole = ROOT_COMPOUND.fullmatch(selector)
    if not selector or not whole:
        return None
    element, tail = whole.groups()
    tests = list(ATTRIBUTE.finditer(tail))
    for test in tests:
        negated, name = test.group(1), test.group(2)
        value = test.group(3) if test.group(3) is not None else test.group(4)
        hit = name in attributes and value in (None, attributes[name])
        if hit == bool(negated):
            return None
    return (int(element == ":root") + len(tests), int(element == "html"))


def resolve(value, scope):
    for _ in range(10):
        value = VAR.sub(lambda m: scope[m.group(1)], value)
    return re.sub(r"\s+", " ", value.strip().lower())


def lotus(attributes, preference):
    env = {**CONSOLE_MEDIA, "prefers-color-scheme": preference}
    applied = []
    for order, (media, selectors, body) in enumerate(
            style_rules(CONSOLE_THEME.read_text())):
        weights = [weight for weight in
                   (root_specificity(selector, attributes) for selector in selectors)
                   if weight is not None]
        if weights and all(media_matches(condition, env) for condition in media):
            applied.append((max(weights), order, body))
    scope = {}
    for _, _, body in sorted(applied, key=lambda rule: rule[:2]):
        scope.update(body)
    return {token: resolve(scope[token], scope) for token in LOTUS_TOKENS}


def docs_declarations(css, selector, width=None):
    merged = {}
    for media, selectors, body in style_rules(css):
        env = {"width": width}
        if selector in selectors and (
                width is None and not media
                or width is not None and all(media_matches(c, env) for c in media)):
            merged.update(body)
    return merged


def yaml_lines(lines):
    stripped = (re.sub(r"(^|\s)#.*$", "", line).rstrip() for line in lines)
    return [line for line in stripped if line.strip()]


def indent(line):
    return len(line) - len(line.lstrip())


def block(lines, key):
    """The key's inline value and nested lines, the key alone at the outer indent."""
    outer = min(indent(line) for line in lines)
    heads = [at for at, line in enumerate(lines)
             if re.fullmatch(rf" {{{outer}}}{key}:(\s.*)?", line)]
    assert len(heads) == 1, f"{key} appears {len(heads)} times"
    head = lines[heads[0]]
    nested = []
    for following in lines[heads[0] + 1:]:
        deeper = indent(following) > outer
        item = indent(following) == outer and following.lstrip().startswith("- ")
        if not (deeper or item):
            break
        nested.append(following)
    return head.split(":", 1)[1].strip(), nested


def items(nested):
    """Split nested lines into list items, each opening with a dash."""
    assert nested and nested[0].lstrip().startswith("- "), nested[:1]
    dash = indent(nested[0])
    groups = []
    for line in nested:
        if indent(line) == dash and line.lstrip().startswith("-"):
            groups.append([])
        groups[-1].append(line)
    return groups


def palette(document):
    theme = block(yaml_lines(document.splitlines()), "theme")[1]
    entries = []
    for entry in items(block(theme, "palette")[1]):
        own = indent(entry[0]) + 2
        lines = [entry[0].replace("-", " ", 1)] + entry[1:]
        entries.append(dict(re.fullmatch(rf" {{{own}}}([\w-]+):\s*(.*)", line).groups()
                            for line in lines if indent(line) == own))
    return entries


def extra_css(document):
    inline, nested = block(yaml_lines(document.splitlines()), "extra_css")
    flow = re.fullmatch(r"\[(.*)\]", inline)
    if flow:
        values = flow.group(1).split(",")
    else:
        assert not inline, inline
        values = []
        for entry in items(nested):
            single = re.fullmatch(r"\s*-\s+([^\s\[{|>&*!][^#]*)", entry[0])
            assert len(entry) == 1 and single, f"unrecognised extra_css entry {entry}"
            values.append(single.group(1))
    return [value.strip().strip("'\"") for value in values if value.strip()]


def under_docs(sheet):
    target = (DOCS / sheet).resolve()
    return "://" not in sheet and target.is_relative_to(DOCS.resolve())


class DocsThemeTests(unittest.TestCase):
    def setUp(self):
        self.mkdocs = (ROOT / "mkdocs.yml").read_text()

    def stylesheet(self):
        sheets = extra_css(self.mkdocs)
        self.assertEqual(len(sheets), 1, sheets)
        return (DOCS / sheets[0]).read_text()

    def test_both_schemes_use_custom_palettes_and_one_docs_stylesheet(self):
        entries = palette(self.mkdocs)
        schemes = sorted(entry.get("scheme") for entry in entries)
        self.assertEqual(schemes, ["default", "slate"])
        for entry in entries:
            self.assertEqual((entry.get("primary"), entry.get("accent")),
                             ("custom", "custom"), entry["scheme"])
        sheets = extra_css(self.mkdocs)
        self.assertEqual(len(sheets), 1, sheets)
        self.assertTrue(under_docs(sheets[0]), sheets[0])
        self.assertTrue((DOCS / sheets[0]).is_file(), sheets[0])

    def test_each_scheme_paints_the_matching_lotus_values(self):
        css = self.stylesheet()
        for scheme, roots in LIVE_ROOTS.items():
            scope = docs_declarations(css, f'[data-md-color-scheme="{scheme}"]')
            for attributes, preference in roots:
                expected = lotus(attributes, preference)
                with self.subTest(scheme=scheme, root=attributes, os=preference):
                    for variable, token in SURFACES.items():
                        self.assertEqual(resolve(scope[variable], scope),
                                         expected[token], variable)
                    for width, surfaces in SIDEBARS.items():
                        for selector in surfaces:
                            painted = docs_declarations(css, selector, width).get(
                                "background-color", "unset")
                            self.assertEqual(
                                resolve(painted, scope),
                                expected["--side"], f"{selector} at {width}px")


if __name__ == "__main__":
    unittest.main()
