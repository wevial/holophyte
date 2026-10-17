import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { ConfigBody, ConfigValues } from "../../src/lib/config";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, NO_ATTENTION, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const OTHER = "http://writer-host:7711";
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const VALUES: ConfigValues = { merge: { mode: "pr", approve: "auto" }, loop: { workers: 2 } };

async function mount(values: ConfigValues = VALUES, hash = "#/settings/general?project=demo") {
  const status = (project: string): Status => ({ ...working, runs: [], project: `/srv/${project}`, host: "writer-host" });
  const poll = peersFetch(ORIGIN, {
    [ORIGIN]: { status: status("demo"), attention: { ...NO_ATTENTION, project: "/srv/demo" } },
    [OTHER]: { status: status("other"), attention: { ...NO_ATTENTION, project: "/srv/other" } },
  });
  const fetch: Fetch = (url, init) => {
    if (!url.endsWith("/config")) return poll(url, init);
    const config: ConfigBody = {
      text: '[merge]\nmode = "pr"\napprove = "auto"\n[loop]\nworkers = 2\n',
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
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={ORIGIN} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
}

const row = (name: string) => screen.getByRole("group", { name });
const workers = () => within(row("Workers")).getByRole("status").textContent;
const unsaved = () => screen.queryByRole("region", { name: "Unsaved changes" });
const tab = (name: RegExp) => within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name });
async function pickTab(name: RegExp) {
  fireEvent.click(tab(name));
  await act(settle);
}
async function pickProject(name: string) {
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: name } });
  await act(settle);
  await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
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

test("changing and restoring workers adds and removes its old value, timing, tab count and unsaved change", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  expect(workers()).toBe("3");
  expect(within(row("Workers")).getByText("was 2")).toBeTruthy();
  expect(within(row("Workers")).getByText("applies at the next loop start")).toBeTruthy();
  expect(tab(/^General/).textContent).toBe("General1");
  expect(unsaved()?.textContent).toContain("1 unsaved change · workers 2 → 3");
  fireEvent.click(screen.getByRole("button", { name: "One fewer worker" }));
  expect(workers()).toBe("2");
  expect(unsaved()).toBeNull();
  expect(tab(/^General/).textContent).toBe("General");
  expect(within(row("Workers")).queryByText("was 2")).toBeNull();
});

test("ordered edits survive tab navigation and Discard restores the file values", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  fireEvent.click(screen.getByRole("button", { name: "Direct to main" }));
  await pickTab(/^Review$/);
  expect(window.location.hash).toBe("#/settings/review?project=demo");
  expect(unsaved()?.textContent).toContain("2 unsaved changes · workers 2 → 3 · mode pr → local");
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
  const input = mode.getByRole("textbox", { name: "Merge mode" }) as HTMLInputElement;
  expect(JSON.parse(input.value)).toEqual({ strategy: "pr" });
  expect(input.readOnly).toBe(true);
  expect(mode.queryAllByRole("button")).toHaveLength(0);
  expect((mode.getByRole("link", { name: "edit this one in Raw TOML" }) as HTMLAnchorElement).hash).toBe("#/settings/toml?project=demo");
});

test("each project keeps its own edits and discarding one leaves the other's draft intact", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  await pickProject("other");
  expect(workers()).toBe("4");
  expect(unsaved()).toBeNull();
  expect(tab(/^General/).textContent).toBe("General");
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  expect(unsaved()?.textContent).toContain("1 unsaved change · workers 4 → 5");
  await pickProject("demo");
  expect(workers()).toBe("3");
  expect(unsaved()?.textContent).toContain("1 unsaved change · workers 2 → 3");
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  await pickProject("other");
  expect(workers()).toBe("5");
  expect(unsaved()?.textContent).toContain("1 unsaved change · workers 4 → 5");
});


test("edits made on the Settings alias survive navigation to an explicit tab", async () => {
  await mount(VALUES, "#/settings");
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  expect(unsaved()?.textContent).toContain("1 unsaved change · workers 2 → 3");
  await pickTab(/^Review$/);
  expect(window.location.hash).toBe("#/settings/review");
  expect(unsaved()?.textContent).toContain("1 unsaved change · workers 2 → 3");
  await pickTab(/^General/);
  expect(workers()).toBe("3");
});

test("workers outside the positive integer shape are read-only with Raw TOML navigation", async () => {
  for (const value of [0, 1.5]) {
    await mount({ merge: { mode: "pr", approve: "auto" }, loop: { workers: value } });
    const field = within(row("Workers"));
    const input = field.queryByRole("textbox", { name: "Workers" }) as HTMLInputElement | null;
    expect(input).not.toBeNull();
    expect(input!.value).toBe(String(value));
    expect(input!.readOnly).toBe(true);
    expect(field.queryAllByRole("button")).toHaveLength(0);
    expect((field.getByRole("link", { name: "edit this one in Raw TOML" }) as HTMLAnchorElement).hash).toBe("#/settings/toml?project=demo");
    cleanup();
  }
});
