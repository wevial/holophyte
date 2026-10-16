import { afterEach, expect, test } from "bun:test";
import { act } from "@testing-library/react";
import type { Root } from "react-dom/client";
import { startClassic } from "../src/entry";
import { VIEW_KEY } from "../src/lib/view";

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
  let app: Root | null = null;
  act(() => {
    app = startClassic(root);
  });
  if (app) started.push(app);
  return { root, started: app != null };
}

const rendersClassic = (root: HTMLElement) => root.querySelector('nav[aria-label="Console"]') != null;

test("with the new view remembered, the classic entry replaces /#/runs with /next.html#/runs and renders nothing", () => {
  localStorage.setItem(VIEW_KEY, "next");
  const { root, started } = startAt("http://console.test/#/runs");
  expect(started).toBe(false);
  expect(root.childNodes.length).toBe(0);
  expect(location.pathname).toBe("/next.html");
  expect(location.hash).toBe("#/runs");
});

test("?classic, nothing stored, or failing storage renders the classic view where it was opened", () => {
  localStorage.setItem(VIEW_KEY, "next");
  const kept = startAt("http://console.test/?classic#view=shipped");
  expect(rendersClassic(kept.root)).toBe(true);
  expect(location.pathname).toBe("/");
  expect(location.hash).toBe("#view=shipped");

  localStorage.clear();
  expect(rendersClassic(startAt("http://console.test/#/runs").root)).toBe(true);
  expect(location.pathname).toBe("/");

  localStorage.setItem(VIEW_KEY, "next");
  const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get: () => {
      throw new Error("storage disabled");
    },
  });
  try {
    expect(rendersClassic(startAt("http://console.test/#/runs").root)).toBe(true);
    expect(location.pathname).toBe("/");
  } finally {
    Object.defineProperty(globalThis, "localStorage", real);
  }
});
