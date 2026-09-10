import { expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { blocksOf, renderMarkdown } from "../../src/lib/markdown";

const html = (body: string) => renderToStaticMarkup(createElement("div", null, renderMarkdown(body)));

/** The DOM of `body` rendered, for element queries. */
const dom = (body: string) => {
  const root = document.createElement("div");
  root.innerHTML = html(body);
  return root;
};

test("the ticket's body: h2, an unchecked list item, a pre, strong, inline code, and raw HTML as literal text", () => {
  const body = "## In scope\n- [ ] one\n\n```\ncode\n```\n**bold** and `x` and a <b>bold</b> tag as raw HTML";
  const root = dom(body);
  expect(root.querySelector("h2")!.textContent).toBe("In scope");
  const item = root.querySelector("li")!;
  expect(item.textContent!.trim()).toBe("one");
  const box = item.querySelector("input[type=checkbox]") as HTMLInputElement;
  expect(box).not.toBeNull();
  expect(box.checked).toBe(false);
  expect(root.querySelector("pre")!.textContent).toBe("code");
  expect(root.querySelector("strong")!.textContent).toBe("bold");
  expect(Array.from(root.querySelectorAll("code")).map((code) => code.textContent)).toContain("x");
  expect(root.querySelector("b")).toBeNull();
  expect(root.textContent).toContain("a <b>bold</b> tag as raw HTML");
});

test("a checked box is checked; a fence swallows markdown until it closes; unknown lines are paragraphs", () => {
  const root = dom("- [x] done\n- plain\n\n```\n# not a heading\n- not a list\n```\n\njust text\nsecond line");
  const items = Array.from(root.querySelectorAll("li"));
  expect(items.map((item) => item.getAttribute("data-checked"))).toEqual(["true", null]);
  expect((items[0]!.querySelector("input") as HTMLInputElement).checked).toBe(true);
  expect(root.querySelector("h1")).toBeNull();
  expect(root.querySelector("pre")!.textContent).toBe("# not a heading\n- not a list");
  const paragraphs = Array.from(root.querySelectorAll("p"));
  expect(paragraphs.length).toBe(1);
  expect(paragraphs[0]!.textContent).toBe("just textsecond line");
  expect(paragraphs[0]!.querySelector("br")).not.toBeNull();
});

test("links are made for http(s) targets only; a script URL stays literal text", () => {
  const root = dom("see [docs](https://example.test/x) and [nope](javascript:alert(1))");
  const links = Array.from(root.querySelectorAll("a"));
  expect(links.map((a) => [a.textContent, a.getAttribute("href"), a.getAttribute("rel")])).toEqual([["docs", "https://example.test/x", "noreferrer noopener"]]);
  expect(root.textContent).toContain("[nope](javascript:alert(1))");
});

test("blocksOf() groups consecutive bullets into one list and blank lines split paragraphs", () => {
  expect(blocksOf("a\nb\n\nc\n- one\n- two\n# H").map((block) => block.kind)).toEqual(["paragraph", "paragraph", "list", "heading"]);
});
