"""The static landing page's links, steps, media set and stylesheet."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "landing" / "public" / "index.html"
DOCS = "https://docs.holophyte.ai/"
REPO = "https://github.com/wevial/holophyte"
MEDIA = re.compile(r"https://media\.holophyte\.ai/demo/[A-Za-z0-9._-]+")

# The upload script derives its file list from the page, so the page keeps
# referencing exactly this set.
MEDIA_BEFORE = {
    "https://media.holophyte.ai/demo/board-crop-dawn.png",
    "https://media.holophyte.ai/demo/board-crop-night.png",
    "https://media.holophyte.ai/demo/board-dawn.png",
    "https://media.holophyte.ai/demo/console-flow-poster.png",
    "https://media.holophyte.ai/demo/console-flow.mp4",
    "https://media.holophyte.ai/demo/console-flow.webm",
    "https://media.holophyte.ai/demo/findings-crop-night.png",
    "https://media.holophyte.ai/demo/findings-crop.png",
    "https://media.holophyte.ai/demo/findings-dawn.png",
    "https://media.holophyte.ai/demo/mark.svg",
    "https://media.holophyte.ai/demo/overview-dawn.png",
    "https://media.holophyte.ai/demo/overview-night.png",
    "https://media.holophyte.ai/demo/pr-row-crop-night.png",
    "https://media.holophyte.ai/demo/pr-row-crop.png",
    "https://media.holophyte.ai/demo/pr-row-dawn.png",
    "https://media.holophyte.ai/demo/run-review-dawn.png",
    "https://media.holophyte.ai/demo/run-review-night.png",
}

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.children, self.parts = tag, dict(attrs), [], []

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def text(self):
        return "".join(p if isinstance(p, str) else p.text() for p in self.parts)

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()

    def find_all(self, tag=None, cls=None):
        return [n for n in self.iter()
                if (tag is None or n.tag == tag)
                and (cls is None or cls in n.classes())]

    def find(self, tag=None, cls=None):
        found = self.find_all(tag, cls)
        if not found:
            raise AssertionError(f"no <{tag} class={cls}> on the page")
        return found[0]


class Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = Node("#document", [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        self.stack[-1].parts.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        self.stack[-1].parts.append(node)

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].parts.append(data)


def page():
    tree = Tree()
    tree.feed(PAGE.read_text())
    tree.close()
    return tree.root


def links(node):
    return [(a.attrs.get("href"), " ".join(a.text().split()))
            for a in node.find_all("a")]


class LandingPageTest(unittest.TestCase):
    def test_top_bar_links_docs_and_repository_beside_an_icon_only_theme_button(self):
        top = page().find("header", "top")
        self.assertIn((DOCS, "Docs"), links(top))
        self.assertIn((REPO, "GitHub"), links(top))
        buttons = top.find_all("button")
        self.assertEqual([b.attrs.get("aria-label") for b in buttons], ["Colour theme"])
        self.assertEqual(buttons[0].text().strip(), "")
        icons = buttons[0].find_all("svg")
        self.assertEqual([svg.attrs.get("aria-hidden") for svg in icons], ["true"])

    def test_hero_offers_the_docs_and_the_repository(self):
        hero = page().find(cls="hero")
        self.assertEqual(links(hero), [(DOCS, "Read the docs"), (REPO, "GitHub")])

    def test_five_numbered_steps_in_order_each_with_a_hidden_icon(self):
        steps = page().find("ol", "steps").find_all("li")
        self.assertEqual([s.find("h3").text().strip() for s in steps],
                         ["Claim", "Implement", "Verify", "Review", "Land"])
        self.assertEqual([s.find(cls="n").text().strip() for s in steps],
                         ["01", "02", "03", "04", "05"])
        for step in steps:
            icons = step.find_all("svg")
            self.assertEqual(len(icons), 1, step.find("h3").text())
            self.assertEqual(icons[0].attrs.get("aria-hidden"), "true")

    def test_page_references_the_same_media_set_the_upload_script_reads(self):
        self.assertEqual(set(MEDIA.findall(PAGE.read_text())), MEDIA_BEFORE)

    def test_stylesheet_has_no_plex_sans_face_and_no_uppercase(self):
        root = page()
        css = "\n".join(s.text() for s in root.find_all("style"))
        fonts = " ".join(link.attrs.get("href", "") for link in root.find_all("link"))
        self.assertTrue(css.strip())
        self.assertNotRegex(css, re.compile(r"IBM\s+Plex\s+Sans", re.I))
        self.assertNotRegex(fonts, re.compile(r"IBM\+Plex\+Sans", re.I))
        self.assertNotRegex(css, re.compile(r"text-transform\s*:\s*uppercase", re.I))


if __name__ == "__main__":
    unittest.main()
