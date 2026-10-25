import { afterEach, expect, test } from "bun:test";
import { act } from "@testing-library/react";
import type { Root } from "react-dom/client";
import { startClassic } from "../src/entry";
import { classicHref } from "../src/lib/view";

const realFetch = globalThis.fetch;
const started: Root[] = [];

afterEach(() => {
  for (const app of started.splice(0)) act(() => app.unmount());
  document.body.innerHTML = "";
  globalThis.fetch = realFetch;
  localStorage.clear();
  location.href = "about:blank";
});

/** Run the classic entry at `href` into a fresh #root, every daemon request
 *  left unanswered, and return the mount point. */
function startAt(href: string) {
  location.href = href;
  globalThis.fetch = (() => new Promise(() => {})) as unknown as typeof fetch;
  const root = document.createElement("div");
  root.id = "root";
  document.body.append(root);
  act(() => {
    started.push(startClassic(root));
  });
  return { root };
}

const rendersClassic = (root: HTMLElement) => root.querySelector('nav[aria-label="Console"]') != null;

test("with the new view stored as remembered, the classic entry renders the classic console where it was opened", () => {
  localStorage.setItem("holophyte.view", "next");
  const { root } = startAt("http://console.test/classic.html#view=board");
  expect(rendersClassic(root)).toBe(true);
  expect(location.pathname).toBe("/classic.html");
  expect(location.hash).toBe("#view=board");
});

test("classicHref reaches the classic page at the given hash", () => {
  expect(classicHref("#run=4&daemon=x")).toBe("/classic.html#run=4&daemon=x");
});
