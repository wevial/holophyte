"""Keep the docs site's Material palette on the console's Lotus theme."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONSOLE_THEME = ROOT / "console" / "src" / "app" / "theme.css"
# The selectors that style the live console root, in rising specificity.
LIVE_ROOT = {"default": (":root",),
             "slate": (":root", ':root[data-theme="dark"]')}
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
# The primary navigation's surfaces: the desktop sidebar and the mobile drawer.
SIDEBARS = (".md-sidebar--primary .md-sidebar__scrollwrap", ".md-nav--primary",
            ".md-nav--primary .md-nav",
            ".md-nav--primary .md-nav__title ~ .md-nav__list")
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*\)")


def rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"@import[^;]*;", "", css)
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = {part.strip() for part in selector.split(",")}
        yield selectors, dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", body))


def declarations(css, matches):
    merged = {}
    for selectors, body in rules(css):
        if matches(selectors):
            merged.update(body)
    return merged


def resolve(value, scope):
    for _ in range(10):
        value = VAR.sub(lambda m: scope[m.group(1)], value)
    return re.sub(r"\s+", " ", value.strip().lower())


def yaml_lines(document):
    return [re.sub(r"\s+#.*$", "", line) for line in document.splitlines()]


def palette(document):
    text = "\n".join(yaml_lines(document)) + "\n"
    found = re.search(r"^  palette:\n((?:    .*\n)+)", text, re.MULTILINE)
    return [dict(re.findall(r"^\s*-?\s*(scheme|primary|accent): (\S+)$",
                            entry, re.MULTILINE))
            for entry in re.split(r"^    - ", found.group(1), flags=re.MULTILINE)
            if entry.strip()]


def extra_css(document):
    text = "\n".join(yaml_lines(document)) + "\n"
    flow = re.search(r"^extra_css:\s*\[(.*)\]\s*$", text, re.MULTILINE)
    if flow:
        return [item.strip().strip("'\"") for item in flow.group(1).split(",")
                if item.strip()]
    found = re.search(r"^extra_css:\s*\n((?:\s+-.*\n)+)", text, re.MULTILINE)
    return ([item.strip().strip("'\"") for item in
             re.findall(r"^\s+-\s*(.+)$", found.group(1), re.MULTILINE)]
            if found else [])


def under_docs(sheet):
    target = (DOCS / sheet).resolve()
    return "://" not in sheet and target.is_relative_to(DOCS.resolve())


def lotus(scheme):
    css = CONSOLE_THEME.read_text()
    scope = {}
    for root in LIVE_ROOT[scheme]:
        scope.update(declarations(css, lambda selectors: root in selectors))
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
