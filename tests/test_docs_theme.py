"""Keep the docs site's Material palette on the console's Lotus theme."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE_THEME = ROOT / "console" / "src" / "app" / "theme.css"
LOTUS_BLOCKS = {"default": '[data-tile="light-lotus"]',
                "slate": '[data-tile="dark-lotus"]'}
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
SIDEBAR = ".md-sidebar--primary .md-sidebar__scrollwrap"
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


def palette(document):
    found = re.search(r"^  palette:\n((?:    .*\n)+)", document, re.MULTILINE)
    return [dict(re.findall(r"^\s+-?\s*(scheme|primary|accent): (\S+)$",
                            entry, re.MULTILINE))
            for entry in re.split(r"^    - ", found.group(1), flags=re.MULTILINE)
            if entry.strip()]


def extra_css(document):
    found = re.search(r"^extra_css:\n((?:  - .*\n)+)", document, re.MULTILINE)
    return re.findall(r"^  - (\S+)$", found.group(1), re.MULTILINE) if found else []


def lotus(scheme):
    css = CONSOLE_THEME.read_text()
    shared = declarations(css, lambda selectors: selectors == {":root"})
    block = declarations(css, lambda selectors: LOTUS_BLOCKS[scheme] in selectors)
    scope = {**shared, **block}
    return {token: resolve(scope[token], scope) for token in LOTUS_TOKENS}


def docs_scope(css, scheme):
    own = f'[data-md-color-scheme="{scheme}"]'
    return declarations(css, lambda selectors: own in selectors)


class DocsThemeTests(unittest.TestCase):
    def setUp(self):
        self.mkdocs = (ROOT / "mkdocs.yml").read_text()

    def stylesheet(self):
        sheets = extra_css(self.mkdocs)
        self.assertEqual(len(sheets), 1, sheets)
        return (ROOT / "docs" / sheets[0]).read_text()

    def test_both_schemes_use_custom_palettes_and_one_docs_stylesheet(self):
        entries = palette(self.mkdocs)
        self.assertEqual({entry["scheme"] for entry in entries}, {"default", "slate"})
        for entry in entries:
            self.assertEqual((entry["primary"], entry["accent"]), ("custom", "custom"),
                             entry["scheme"])
        sheets = extra_css(self.mkdocs)
        self.assertEqual(len(sheets), 1, sheets)
        self.assertTrue((ROOT / "docs" / sheets[0]).is_file(), sheets[0])

    def test_each_scheme_paints_the_matching_lotus_values(self):
        css = self.stylesheet()
        sidebar = declarations(css, lambda selectors: SIDEBAR in selectors)
        for scheme in LOTUS_BLOCKS:
            expected = lotus(scheme)
            scope = docs_scope(css, scheme)
            with self.subTest(scheme=scheme):
                for variable, token in SURFACES.items():
                    self.assertEqual(resolve(scope[variable], scope), expected[token],
                                     f"{scheme} {variable}")
                self.assertEqual(resolve(sidebar["background-color"], scope),
                                 expected["--side"], f"{scheme} sidebar")


if __name__ == "__main__":
    unittest.main()
