import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { patchLines } from "../../src/next/settings";
import type { ConfigBody } from "../../src/lib/config";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, NO_ATTENTION, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const R1 = "3f9a2c1" + "a".repeat(57);
const R2 = "92b73d4" + "b".repeat(57);
const SAVED = new Date(2026, 9, 14, 9, 12).getTime();
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const conflict = "config.toml changed on writer-host since this page read it. It has been read again: check the fields and save once more.";
const refusal = "[holo2] /x/config.toml: [loop] workers must be an integer >= 1, got 0";

async function mount(options: { status?: number; error?: string; configEdit?: boolean; modified?: number; actor?: ConfigBody["saved"]; pending?: Promise<void>; } = {}) {
  let config: ConfigBody = {
    text: '[loop]\nworkers = 2\n', values: { loop: { workers: 2 } }, path: "/srv/demo/config.toml",
    applies: "next loop start", revision: R1, saved: options.actor === undefined ? { at_ms: SAVED, actor: "maintainer" } : options.actor,
    modified_ms: options.modified ?? SAVED + 1000,
  };
  const status: Status = { ...working, project: "/srv/demo", host: "writer-host", runs: [], config_edit: options.configEdit ?? true, actions: true };
  const poll = peersFetch(ORIGIN, { [ORIGIN]: { status, attention: { ...NO_ATTENTION, project: status.project } } });
  const writes: RequestInit[] = [];
  const actions: string[] = [];
  let reads = 0;
  const fetch: Fetch = async (url, init) => {
    if (url.includes("/actions/")) {
      actions.push(url);
      return Response.json({ ok: true, detail: "Restart requested" });
    }
    if (!url.endsWith("/config")) return poll(url, init);
    if (init?.method !== "PUT") { reads++; return Response.json(config); }
    writes.push(init);
    await options.pending;
    if (options.status === 409) {
      config = { ...config, revision: R2, values: { loop: { workers: 4 } } };
      return Response.json({ current: R2 }, { status: 409 });
    }
    if (options.status && options.status !== 200) return Response.json({ error: options.error }, { status: options.status });
    config = { ...config, revision: R2, values: { loop: { workers: 3 } } };
    return Response.json({ revision: R2, applies: "next loop start", backup: "/srv/demo/config.toml.bak" });
  };
  window.location.hash = "#/settings/general?project=demo";
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={ORIGIN} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return { writes, actions, reads: () => reads };
}

function edit() { fireEvent.click(screen.getByRole("button", { name: "One more worker" })); }
function review() { fireEvent.click(screen.getByRole("button", { name: "Review and save" })); }
const dialog = () => screen.getByRole("dialog", { name: "Save these changes?" });
const unsaved = () => screen.queryByRole("region", { name: "Unsaved changes" });
async function save() {
  fireEvent.click(within(dialog()).getByRole("button", { name: "Save" }));
  await act(settle);
  await act(settle);
}
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("review lists the old and new TOML lines in order and Keep editing preserves the draft without writing", async () => {
  const source = await mount();
  edit(); review();
  const confirm = within(dialog());
  expect(confirm.getByText("Save · holophyte")).toBeTruthy();
  expect(confirm.getByText("These lines change in config.toml on writer-host.")).toBeTruthy();
  expect(dialog().textContent).toMatch(/\[loop\].*− workers = 2.*\+ workers = 3/);
  expect(confirm.getByText("Applies at the next loop start.")).toBeTruthy();
  expect(confirm.getByText("Runs in flight keep the settings they started with.")).toBeTruthy();
  fireEvent.click(confirm.getByRole("button", { name: "Keep editing" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(unsaved()?.textContent).toContain("workers 2 → 3");
  expect(source.writes).toHaveLength(0);
});

test("save sends one revision-checked patch, re-reads config, clears the draft and offers a working restart and Later", async () => {
  const source = await mount();
  const reads = source.reads();
  edit(); review(); await save();
  expect(source.writes).toHaveLength(1);
  expect(new Headers(source.writes[0]!.headers).get("If-Match")).toBe(R1);
  expect(JSON.parse(String(source.writes[0]!.body))).toEqual({ patch: { "loop.workers": 3 } });
  expect(source.reads()).toBeGreaterThan(reads);
  expect(unsaved()).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  const banner = screen.getAllByRole("status").find((item) => item.textContent?.includes("Saved config.toml at revision"));
  expect(banner).toBeTruthy();
  expect(banner!.textContent).toContain("Saved config.toml at revision 92b73d4.");
  expect(banner!.textContent).toContain("It applies at the next loop start.");
  fireEvent.click(within(banner!).getByRole("button", { name: "Restart supervisor" }));
  await act(settle);
  await act(settle);
  expect(source.actions).toEqual([`${ORIGIN}/actions/restart-supervisor`]);
  fireEvent.click(within(banner!).getByRole("button", { name: "Later" }));
  expect(screen.queryByText(/Saved config.toml at revision/)).toBeNull();
});

test.each([4, 2])("a completed save preserves the newer Workers value %s after Back and Forward project navigation", async (value) => {
  let complete!: () => void;
  const pending = new Promise<void>((resolve) => { complete = resolve; });
  const source = await mount({ pending });
  await act(async () => { window.location.hash = "#/settings/general?project=other"; await settle(); });
  await act(async () => { window.location.hash = "#/settings/general?project=demo"; await settle(); });
  edit(); review(); await save();
  expect(source.writes).toHaveLength(1);
  await act(async () => { window.history.back(); await settle(); });
  expect(screen.getByText("other · config.toml on its host")).toBeTruthy();
  await act(async () => { window.history.forward(); await settle(); });
  fireEvent.click(screen.getByRole("button", { name: value === 4 ? "One more worker" : "One fewer worker" }));
  expect(within(screen.getByRole("group", { name: "Workers" })).getByRole("status").textContent).toBe(String(value));
  await act(async () => { complete(); await settle(); });
  await act(settle);
  expect(within(screen.getByRole("group", { name: "Workers" })).getByRole("status").textContent).toBe(String(value));
  expect(unsaved()?.textContent).toContain(`workers 3 → ${value}`);
  review();
  expect(dialog().textContent).toMatch(new RegExp(`− workers = 3.*\\+ workers = ${value}`));
  await save();
  expect(new Headers(source.writes[1]!.headers).get("If-Match")).toBe(R2);
  expect(JSON.parse(String(source.writes[1]!.body))).toEqual({ patch: { "loop.workers": value } });
});

test("a revision conflict re-reads the file, keeps edits and uses the refreshed revision for a deliberate retry", async () => {
  const source = await mount({ status: 409 });
  const reads = source.reads();
  edit(); review(); await save();
  expect(within(dialog()).getByText(conflict)).toBeTruthy();
  expect(source.reads()).toBeGreaterThan(reads);
  expect(within(screen.getByRole("group", { name: "Workers" })).getByRole("status").textContent).toBe("3");
  expect(unsaved()).toBeTruthy();
  expect(source.writes).toHaveLength(1);
  await save();
  expect(new Headers(source.writes[1]!.headers).get("If-Match")).toBe(R2);
  expect(JSON.parse(String(source.writes[1]!.body))).toEqual({ patch: { "loop.workers": 3 } });
});

test("a named refusal closes the dialog and places the daemon sentence under Workers while preserving the draft", async () => {
  const source = await mount({ status: 400, error: refusal });
  const reads = source.reads();
  edit(); review(); await save();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(within(screen.getByRole("group", { name: "Workers" })).getByRole("alert").textContent).toBe(refusal);
  expect(unsaved()).toBeTruthy();
  expect(source.reads()).toBeGreaterThan(reads);
  expect((screen.getByRole("button", { name: "Review and save" }) as HTMLButtonElement).disabled).toBe(true);
});

test("unmapped refusals and other failures stay in the dialog with the draft and re-read config", async () => {
  for (const status of [400, 503]) {
    const source = await mount({ status, error: "The writer cannot save this file." });
    const reads = source.reads();
    edit(); review(); await save();
    expect(dialog().textContent).toContain("The writer cannot save this file.");
    expect(unsaved()).toBeTruthy();
    expect(source.reads()).toBeGreaterThan(reads);
    cleanup();
  }
});

test("a daemon without config edits disables Review and save", async () => {
  const source = await mount({ configEdit: false });
  edit();
  expect((screen.getByRole("button", { name: "Review and save" }) as HTMLButtonElement).disabled).toBe(true);
  expect(source.writes).toHaveLength(0);
});

test("the head names the revision and last saver until a host modification is more than five seconds later", async () => {
  await mount();
  expect(screen.getByText("demo · config.toml on writer-host · revision 3f9a2c1 · saved Oct 14 09:12 by you")).toBeTruthy();
  cleanup();
  await mount({ modified: new Date(2026, 9, 15, 10, 0).getTime() });
  expect(screen.getByText("demo · config.toml on writer-host · revision 3f9a2c1 · changed on the host Oct 15 10:00")).toBeTruthy();
});

test("the head identifies operator and supervisor saves, omits absent actors and identifies files without save records as changed on host", async () => {
  for (const [actor, suffix] of [["operator", " by the operator seat"], ["supervisor", " by the supervisor"], [null, ""]] as const) {
    await mount({ actor: { at_ms: SAVED, actor }, modified: SAVED + 5000 });
    expect(screen.getByText(`demo · config.toml on writer-host · revision 3f9a2c1 · saved Oct 14 09:12${suffix}`)).toBeTruthy();
    cleanup();
  }
  await mount({ actor: null, modified: SAVED });
  expect(screen.getByText("demo · config.toml on writer-host · revision 3f9a2c1 · changed on the host Oct 14 09:12")).toBeTruthy();
});

test("discarding a refused draft clears its field errors so a new draft can be reviewed", async () => {
  await mount({ status: 400, error: refusal });
  edit(); review(); await save();
  expect(within(screen.getByRole("group", { name: "Workers" })).getByRole("alert").textContent).toBe(refusal);
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(unsaved()).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Human" }));
  expect((screen.getByRole("button", { name: "Review and save" }) as HTMLButtonElement).disabled).toBe(false);
  expect(within(screen.getByRole("group", { name: "Workers" })).queryByRole("alert")).toBeNull();
  review();
  expect(dialog().textContent).toContain('+ approve = "human"');
});


test("the review diff follows file table and key order, quotes TOML values and adds absent keys without a removal", () => {
  expect(patchLines(
    { review: { enabled: false, labels: ["old"], unchanged: "keep" }, agents: { command: 'echo "old"' } },
    {
      "agents.command": { old: 'echo "old"', value: 'echo "new"' },
      "review.labels": { old: ["old"], value: ["a", "b"] },
      "review.enabled": { old: false, value: true },
      "review.extra": { old: "default", value: "added" },
    },
  )).toEqual([
    { kind: "context", text: "[review]" },
    { kind: "removed", text: "enabled = false" },
    { kind: "added", text: "enabled = true" },
    { kind: "removed", text: 'labels = ["old"]' },
    { kind: "added", text: 'labels = ["a", "b"]' },
    { kind: "added", text: 'extra = "added"' },
    { kind: "context", text: "[agents]" },
    { kind: "removed", text: 'command = "echo \\"old\\""' },
    { kind: "added", text: 'command = "echo \\"new\\""' },
  ]);
});
