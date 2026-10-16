import { afterEach, expect, test } from "bun:test";
import { VIEW_KEY, openRememberedView } from "../src/lib/view";

afterEach(() => {
  localStorage.clear();
  location.href = "about:blank";
});

test("with the new view remembered, the classic entry replaces /#/runs with /next.html#/runs and does not render", () => {
  localStorage.setItem(VIEW_KEY, "next");
  location.href = "http://console.test/#/runs";
  expect(openRememberedView()).toBe(true);
  expect(location.pathname).toBe("/next.html");
  expect(location.hash).toBe("#/runs");
});

test("?classic, nothing stored, or failing storage leaves the classic entry where it is to render", () => {
  localStorage.setItem(VIEW_KEY, "next");
  location.href = "http://console.test/?classic#/runs";
  expect(openRememberedView()).toBe(false);
  expect(location.pathname).toBe("/");

  localStorage.clear();
  location.href = "http://console.test/#/runs";
  expect(openRememberedView()).toBe(false);
  expect(location.pathname).toBe("/");

  const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get: () => {
      throw new Error("storage disabled");
    },
  });
  try {
    expect(openRememberedView()).toBe(false);
    expect(location.pathname).toBe("/");
  } finally {
    Object.defineProperty(globalThis, "localStorage", real);
  }
});
