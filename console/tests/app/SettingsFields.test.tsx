import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { ConfigBody, ConfigValues } from "../../src/lib/config";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, NO_ATTENTION, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const OTHER = "http://writer-host:7711";
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const VALUES: ConfigValues = { merge: { mode: "pr", approve: "auto" }, loop: { workers: 2 } };

async function mount(values: ConfigValues | null = VALUES, hash = "#/settings/general?project=demo") {
  const status = (project: string): Status => ({ ...working, runs: [], project: `/srv/${project}`, host: "writer-host", config_edit: true });
  const poll = peersFetch(ORIGIN, {
    [ORIGIN]: { status: status("demo"), attention: { ...NO_ATTENTION, project: "/srv/demo" } },
    [OTHER]: { status: status("other"), attention: { ...NO_ATTENTION, project: "/srv/other" } },
  });
  const writes: unknown[] = [];
  const fetch: Fetch = (url, init) => {
    if (!url.endsWith("/config")) return poll(url, init);
    if (init?.method === "PUT") {
      writes.push(JSON.parse(String(init.body)));
      return Promise.resolve(Response.json({ revision: "e".repeat(64), applies: "next loop start" }));
    }
    const config: ConfigBody = {
      text: values === null ? '[merge\n' : '[merge]\nmode = "pr"\napprove = "auto"\n[loop]\nworkers = 2\n',
      values: url.startsWith(OTHER) ? { merge: { mode: "local", approve: "human" }, loop: { workers: 4 } } : values,
      path: `/srv/${url.startsWith(OTHER) ? "other" : "demo"}/config.toml`,
      applies: "next loop start",
      revision: "f".repeat(64),
      modified_ms: 1791969120000,
      saved: { at_ms: 1791969120000, actor: "maintainer" },
    };
    return Promise.resolve(Response.json(config));
  };
  window.location.hash = hash;
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={ORIGIN} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return writes;
}

const row = (name: string) => screen.getByRole("group", { name });
const workers = () => within(row("Workers")).getByRole("status").textContent;
const unsaved = () => screen.queryByRole("region", { name: "Unsaved changes" });
const count = () => unsaved()?.querySelector("p")?.textContent;
const changeLine = (label: string) => within(row(label)).queryByText(/ → .* · applies at the /)?.textContent;
const tab = (name: RegExp) => within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name });
async function pickTab(name: RegExp) {
  fireEvent.click(tab(name));
  await act(settle);
}
async function pickProject(name: string) {
  fireEvent.click(screen.getByRole("button", { name: /Switch project$/ }));
  await act(settle);
  fireEvent.click(within(screen.getByRole("listbox", { name: "Projects" })).getAllByRole("option").find((option) => option.textContent?.startsWith(name))!);
  await act(settle);
  await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("General shows the three ordered file values without change marks", async () => {
  await mount();
  const rows = screen.getAllByRole("group").filter((item) => ["Merge mode", "Approve", "Workers"].some((label) => item === row(label)));
  expect(rows).toEqual([row("Merge mode"), row("Approve"), row("Workers")]);
  for (const [label, key] of [["Merge mode", "merge.mode"], ["Approve", "merge.approve"], ["Workers", "loop.workers"]]) {
    expect(within(row(label!)).getByText(key!)).toBeTruthy();
    expect(row(label!).textContent).not.toContain("was ");
  }
  expect(screen.getByRole("button", { name: "Pull request" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: "Auto" }).getAttribute("aria-pressed")).toBe("true");
  expect(workers()).toBe("2");
  expect(screen.getByText("Tickets this project runs at once on writer-host.")).toBeTruthy();
  expect(unsaved()).toBeNull();
});

test("unparsable configuration shows an explanation without editable defaults", async () => {
  await mount(null);
  expect(screen.queryByRole("button", { name: "One more worker" })).toBeNull();
  expect(screen.queryByRole("button", { name: "One fewer worker" })).toBeNull();
  for (const name of ["Direct to main", "Pull request", "Auto", "Human"]) {
    expect(screen.queryByRole("button", { name })).toBeNull();
  }
  expect(screen.queryByText("default")).toBeNull();
  expect(screen.getByText("The file does not parse as TOML. Settings cannot be edited until the file is fixed.")).toBeTruthy();
  expect(unsaved()).toBeNull();
});

test("changing and restoring workers adds and removes its from → to line, tab count and unsaved change", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  expect(workers()).toBe("3");
  expect(changeLine("Workers")).toBe("2 → 3 · applies at the next loop start");
  expect(tab(/^General/).textContent).toBe("General1");
  expect(count()).toBe("1 change");
  fireEvent.click(screen.getByRole("button", { name: "One fewer worker" }));
  expect(workers()).toBe("2");
  expect(unsaved()).toBeNull();
  expect(tab(/^General/).textContent).toBe("General");
  expect(changeLine("Workers")).toBeUndefined();
});

test("ordered edits survive tab navigation and Discard restores the file values", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  fireEvent.click(screen.getByRole("button", { name: "Direct to main" }));
  await pickTab(/^Review$/);
  expect(window.location.hash).toBe("#/settings/review?project=demo");
  expect(count()).toBe("2 changes");
  expect(tab(/^General/).textContent).toBe("General2");
  await pickTab(/^General/);
  expect(workers()).toBe("3");
  expect(screen.getByRole("button", { name: "Direct to main" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(unsaved()).toBeNull();
  expect(workers()).toBe("2");
  expect(screen.getByRole("button", { name: "Pull request" }).getAttribute("aria-pressed")).toBe("true");
});

test("missing workers uses its bounded default and an incompatible merge value links to project Raw TOML read-only", async () => {
  await mount({ merge: { mode: { strategy: "pr" }, approve: "auto" } });
  expect(workers()).toBe("1");
  expect(within(row("Workers")).getByText("default")).toBeTruthy();
  expect((screen.getByRole("button", { name: "One fewer worker" }) as HTMLButtonElement).disabled).toBe(true);
  const mode = within(row("Merge mode"));
  expect(JSON.parse(mode.getByLabelText("Merge mode").textContent!)).toEqual({ strategy: "pr" });
  expect(mode.queryByRole("textbox")).toBeNull();
  expect(mode.queryAllByRole("button")).toHaveLength(0);
  expect((mode.getByRole("link", { name: "Edit in Raw TOML" }) as HTMLAnchorElement).hash).toBe("#/settings/toml?project=demo");
});

test("each project keeps its own edits and discarding one leaves the other's draft intact", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  await pickProject("other");
  expect(workers()).toBe("4");
  expect(unsaved()).toBeNull();
  expect(tab(/^General/).textContent).toBe("General");
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  expect(changeLine("Workers")).toBe("4 → 5 · applies at the next loop start");
  await pickProject("demo");
  expect(workers()).toBe("3");
  expect(changeLine("Workers")).toBe("2 → 3 · applies at the next loop start");
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  await pickProject("other");
  expect(workers()).toBe("5");
  expect(count()).toBe("1 change");
  expect(changeLine("Workers")).toBe("4 → 5 · applies at the next loop start");
});


test("edits made on the Settings alias survive navigation to an explicit tab", async () => {
  await mount(VALUES, "#/settings");
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  expect(count()).toBe("1 change");
  await pickTab(/^Review$/);
  expect(window.location.hash).toBe("#/settings/review");
  expect(count()).toBe("1 change");
  await pickTab(/^General/);
  expect(workers()).toBe("3");
});

test("workers outside the positive integer shape are read-only with Raw TOML navigation", async () => {
  for (const value of [0, 1.5]) {
    await mount({ merge: { mode: "pr", approve: "auto" }, loop: { workers: value } });
    const field = within(row("Workers"));
    expect(field.getByLabelText("Workers").textContent).toBe(String(value));
    expect(field.queryByRole("textbox")).toBeNull();
    expect(field.queryAllByRole("button")).toHaveLength(0);
    expect((field.getByRole("link", { name: "Edit in Raw TOML" }) as HTMLAnchorElement).hash).toBe("#/settings/toml?project=demo");
    cleanup();
  }
});

test("General's Colour row offers Automatic and four colours as round swatch buttons named by label, and picking Teal is an edit that saves console.colour", async () => {
  const writes = await mount();
  const colour = row("Colour");
  const buttons = within(colour).getAllByRole("button");
  expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual(["Automatic", "Violet", "Teal", "Green", "Blue"]);
  expect(buttons.filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.getAttribute("aria-label"))).toEqual(["Automatic"]);
  expect(buttons.map((button) => button.style.background)).toEqual([
    expect.stringMatching(/^conic-gradient\(var\(--p-violet\).*var\(--p-teal\).*var\(--p-green\).*var\(--p-blue\)/),
    "var(--p-violet)",
    "var(--p-teal)",
    "var(--p-green)",
    "var(--p-blue)",
  ]);
  expect(buttons.every((button) => button.classList.contains("rounded-full"))).toBe(true);

  fireEvent.click(within(colour).getByRole("button", { name: "Teal" }));
  expect(within(row("Colour")).getByRole("button", { name: "Teal" }).getAttribute("aria-pressed")).toBe("true");
  expect(changeLine("Colour")).toBe("Automatic → Teal · applies at the next loop start");
  fireEvent.click(within(unsaved()!).getByRole("button", { name: "Save" }));
  fireEvent.click(within(screen.getByRole("alertdialog", { name: "Save these changes?" })).getByRole("button", { name: "Save" }));
  await act(settle);
  await act(settle);
  expect(writes).toEqual([{ patch: { "console.colour": "teal" } }]);
});

test("Merge mode's choices are one group of segment buttons with only the current one pressed", async () => {
  await mount();
  const segments = within(row("Merge mode")).getAllByRole("button");
  expect(segments.map((button) => [button.textContent, button.getAttribute("aria-pressed")])).toEqual([["Direct to main", "false"], ["Pull request", "true"]]);
  expect(new Set(segments.map((button) => button.parentElement)).size).toBe(1);
  fireEvent.click(segments[0]!);
  expect(segments.map((button) => button.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
});

test("switching Approve from Auto to Human reads Auto → Human and when it applies, with no was note", async () => {
  await mount();
  fireEvent.click(within(row("Approve")).getByRole("button", { name: "Human" }));
  expect(changeLine("Approve")).toBe("Auto → Human · applies at the next loop start");
  expect(row("Approve").textContent).not.toContain("was ");
});

test("a row's config key is in the row but shows only while the row is hovered or holds focus", async () => {
  await mount();
  const key = within(row("Approve")).getByText("merge.approve");
  const opacity = () => getComputedStyle(key).opacity;
  expect(opacity()).toBe("0");
  fireEvent.mouseEnter(row("Approve"));
  expect(opacity()).toBe("1");
  fireEvent.mouseLeave(row("Approve"));
  expect(opacity()).toBe("0");
  const human = within(row("Approve")).getByRole("button", { name: "Human" });
  act(() => human.focus());
  expect(opacity()).toBe("1");
  act(() => human.blur());
  expect(opacity()).toBe("0");
  expect(getComputedStyle(within(row("Merge mode")).getByText("merge.mode")).opacity).toBe("0");
});
