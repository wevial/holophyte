import { describe, expect, test } from "bun:test";

import { menuTemplate } from "../menu.ts";

const labels = (items: { label?: string; type?: string }[]) =>
  items.filter((i) => i.type !== "separator").map((i) => i.label);

describe("menuTemplate", () => {
  test("holds Show console, a checked Open at login checkbox, Developer tools and Quit, in that order", () => {
    const items = menuTemplate({ openAtLogin: true });
    expect(labels(items)).toEqual(["Show console", "Open at login", "Developer tools", "Quit"]);
    const toggle = items.find((i) => i.label === "Open at login");
    expect(toggle).toMatchObject({ type: "checkbox", checked: true });
  });

  test("the checkbox is unchecked when the login item is off", () => {
    const toggle = menuTemplate({ openAtLogin: false }).find((i) => i.label === "Open at login");
    expect(toggle).toMatchObject({ type: "checkbox", checked: false });
  });

  test("Developer tools sits before the separator and clicking it calls toggleDevTools", () => {
    let calls = 0;
    const items = menuTemplate({ openAtLogin: false }, { toggleDevTools: () => (calls += 1) });
    expect(items.map((i) => i.type ?? i.label)).toEqual([
      "Show console",
      "checkbox",
      "Developer tools",
      "separator",
      "Quit",
    ]);
    items.find((i) => i.label === "Developer tools")?.click?.({ checked: false });
    expect(calls).toBe(1);
  });

  test("clicking the checkbox passes its new state to the login-item action", () => {
    const seen: boolean[] = [];
    const toggle = menuTemplate({ openAtLogin: false }, { setOpenAtLogin: (v) => seen.push(v) }).find(
      (i) => i.label === "Open at login",
    );
    toggle?.click?.({ checked: true });
    expect(seen).toEqual([true]);
  });
});
