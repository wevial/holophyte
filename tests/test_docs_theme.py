"""Keep the docs site's Material palette on the console's Lotus theme."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONSOLE_THEME = ROOT / "console" / "src" / "app" / "theme.css"
# The live console root's attributes under the Lotus theme in each scheme.
LIVE_ROOT = {"default": {"data-theme": "light"}, "slate": {"data-theme": "dark"}}
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
# Every sidebar surface: navigation and contents columns, and the mobile drawer.
SIDEBARS = (".md-sidebar--primary .md-sidebar__scrollwrap",
            ".md-nav--primary > .md-nav__title",
            ".md-sidebar--secondary .md-sidebar__scrollwrap",
            ".md-sidebar--secondary .md-nav__title", ".md-nav--primary",
            ".md-nav--primary .md-nav",
            ".md-nav--primary .md-nav__title ~ .md-nav__list")
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
ROOT_SELECTOR = re.compile(r":root((?:\[[^\]]+\]|:not\(\[[^\]]+\]\))*)")
ATTRIBUTE = re.compile(r'(:not\()?\[([\w-]+)(?:=(?:"([^"]*)"|([\w-]+)))?\]')


def rules(css):
    """Yield (top_level, selectors, declarations) for every innermost rule."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"@import[^;{]*;", "", css)
    depth, end = 0, 0
    for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
        between = css[end:match.start()]
        depth += between.count("{") - between.count("}")
        end = match.end()
        selectors = {part.strip() for part in match.group(1).split(",")}
        yield depth == 0, selectors, parse_declarations(match.group(2))


def parse_declarations(body):
    found = {}
    for part in re.split(r";(?![^(]*\))", body):
        pair = re.fullmatch(r"\s*([\w-]+)\s*:\s*(.*?)\s*", part, re.DOTALL)
        if pair:
            found[pair.group(1)] = pair.group(2)
    return found


def declarations(css, matches):
    merged = {}
    for _, selectors, body in rules(css):
        if matches(selectors):
            merged.update(body)
    return merged


def root_specificity(selector, attributes):
    """A `:root` compound's specificity when it matches the attributes, else None."""
    whole = ROOT_SELECTOR.fullmatch(selector)
    if not whole:
        return None
    tests = list(ATTRIBUTE.finditer(whole.group(1)))
    for test in tests:
        negated, name = test.group(1), test.group(2)
        value = test.group(3) if test.group(3) is not None else test.group(4)
        hit = name in attributes and value in (None, attributes[name])
        if hit == bool(negated):
            return None
    return 1 + len(tests)


def resolve(value, scope):
    for _ in range(10):
        value = VAR.sub(lambda m: scope[m.group(1)], value)
    return re.sub(r"\s+", " ", value.strip().lower())


def yaml_lines(document):
    stripped = (re.sub(r"(^|\s)#.*$", "", line).rstrip()
                for line in document.splitlines())
    return [line for line in stripped if line.strip()]


def indent(line):
    return len(line) - len(line.lstrip())


def block(document, key):
    """The key's inline value and the lines nested under it."""
    lines = yaml_lines(document)
    for at, line in enumerate(lines):
        head = re.fullmatch(rf"\s*{key}:\s*(.*)", line)
        if not head:
            continue
        nested = []
        for following in lines[at + 1:]:
            deeper = indent(following) > indent(line)
            item = indent(following) == indent(line) and following.lstrip()[:1] == "-"
            if not (deeper or item):
                break
            nested.append(following)
        return head.group(1), nested
    return "", []


def items(nested):
    """Split nested lines into list items at their outermost dash."""
    dashes = [indent(line) for line in nested if line.lstrip().startswith("-")]
    groups = []
    for line in nested:
        if dashes and indent(line) == min(dashes) and line.lstrip().startswith("-"):
            groups.append([])
        if groups:
            groups[-1].append(line)
    return groups


def palette(document):
    return [dict(re.findall(r"^\s*-?\s*(scheme|primary|accent):\s*(\S+)$",
                            "\n".join(entry), re.MULTILINE))
            for entry in items(block(document, "palette")[1])]


def extra_css(document):
    inline, nested = block(document, "extra_css")
    flow = re.fullmatch(r"\[(.*)\]", inline)
    values = (flow.group(1).split(",") if flow
              else [entry[0].lstrip()[1:] for entry in items(nested)])
    return [value.strip().strip("'\"") for value in values if value.strip()]


def under_docs(sheet):
    target = (DOCS / sheet).resolve()
    return "://" not in sheet and target.is_relative_to(DOCS.resolve())


def lotus(scheme):
    applied = []
    for order, (top_level, selectors, body) in enumerate(
            rules(CONSOLE_THEME.read_text())):
        weights = [weight for weight in
                   (root_specificity(selector, LIVE_ROOT[scheme])
                    for selector in selectors) if weight is not None]
        if top_level and weights:
            applied.append((max(weights), order, body))
    scope = {}
    for _, _, body in sorted(applied, key=lambda rule: rule[:2]):
        scope.update(body)
    return {token: resolve(scope[token], scope) for token in LOTUS_TOKENS}


class DocsThemeTests(unittest.TestCase):
    def setUp(self):
        self.mkdocs = (ROOT / "mkdocs.yml").read_text()

    def stylesheet(self):
        sheets = extra_css(self.mkdocs)
        self.assertEqual(len(sheets), 1, sheets)
        return (DOCS / sheets[0]).read_text()

    def test_both_schemes_use_custom_palettes_and_one_docs_stylesheet(self):
        entries = palette(self.mkdocs)
        schemes = sorted(entry["scheme"] for entry in entries)
        self.assertEqual(schemes, ["default", "slate"])
        for entry in entries:
            self.assertEqual((entry["primary"], entry["accent"]), ("custom", "custom"),
                             entry["scheme"])
        sheets = extra_css(self.mkdocs)
        self.assertEqual(len(sheets), 1, sheets)
        self.assertTrue(under_docs(sheets[0]), sheets[0])
        self.assertTrue((DOCS / sheets[0]).is_file(), sheets[0])

    def test_each_scheme_paints_the_matching_lotus_values(self):
        css = self.stylesheet()
        sidebars = {selector: declarations(css, lambda selectors: selector in selectors)
                    for selector in SIDEBARS}
        for scheme in LIVE_ROOT:
            expected = lotus(scheme)
            own = f'[data-md-color-scheme="{scheme}"]'
            scope = declarations(css, lambda selectors: own in selectors)
            with self.subTest(scheme=scheme):
                for variable, token in SURFACES.items():
                    self.assertEqual(resolve(scope[variable], scope), expected[token],
                                     f"{scheme} {variable}")
                for selector, sidebar in sidebars.items():
                    self.assertEqual(resolve(sidebar["background-color"], scope),
                                     expected["--side"], f"{scheme} {selector}")


if __name__ == "__main__":
    unittest.main()
