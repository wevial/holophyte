import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { lineDiff } from "../../src/next/settings";
import { NextConsole } from "../../src/next/router";
import type { ConfigBody } from "../../src/lib/config";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, NO_ATTENTION, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const REVISION = "3f9a2c1b" + "a".repeat(56);
const ORIGINAL = "[loop]\nworkers = 2\n";
const CLEAN = "[loop]\nworkers = 3\n";
const INVALID = "[loop]\nworkers = 0\n";
const SENTENCE = "[loop] workers must be an integer >= 1, got 0";
const REFUSAL = { error: `[holo2] /x/config.toml: ${SENTENCE}`, line: 2 };
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;

async function mount(options: { tab?: string; check?: (text: string) => Promise<Response>; saveRefused?: boolean } = {}) {
  let config: ConfigBody = { text: ORIGINAL, values: { loop: { workers: 2 } }, path: "/srv/demo/config.toml", applies: "next loop start", revision: REVISION, saved: null, modified_ms: null };
  const status: Status = { ...working, project: "/srv/demo", host: "writer-host", runs: [], config_edit: true, actions: true };
  const poll = peersFetch(ORIGIN, { [ORIGIN]: { status, attention: { ...NO_ATTENTION, project: status.project } } });
  const checks: RequestInit[] = [];
  const writes: RequestInit[] = [];
  const fetch: Fetch = async (url, init) => {
    if (!url.endsWith("/config")) return poll(url, init);
    if (init?.method !== "PUT") return Response.json(config);
    const body = JSON.parse(String(init.body)) as { text: string; check?: boolean };
    if (body.check) {
      checks.push(init);
      return options.check ? options.check(body.text) : body.text === INVALID ? Response.json(REFUSAL, { status: 400 }) : Response.json({ ok: true });
    }
    writes.push(init);
    if (options.saveRefused) return Response.json(REFUSAL, { status: 400 });
    config = { ...config, text: body.text, revision: "92b73d4" + "b".repeat(57), values: { loop: { workers: 3 } } };
    return Response.json({ revision: config.revision, applies: config.applies, backup: "/srv/demo/config.toml.bak" });
  };
  window.location.hash = `#/settings/${options.tab ?? "toml"}?project=demo`;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={ORIGIN} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return { checks, writes };
}
const editor = () => screen.getByRole("textbox", { name: "config.toml for demo" }) as HTMLTextAreaElement;
const saveButton = () => screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
const change = (text: string) => fireEvent.change(editor(), { target: { value: text } });
const checked = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 550)); await settle(); });
async function tab(name: string) { fireEvent.click(screen.getByRole("link", { name })); await act(settle); }
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("Raw TOML opens the whole file with a numbered gutter and revision revert, and an unchanged file cannot save", async () => {
  const source = await mount();
  expect(editor().value).toBe(ORIGINAL);
  const gutter = editor().parentElement!.querySelector('[aria-hidden="true"]')!;
  expect(gutter).toBeTruthy();
  expect(gutter.textContent?.replace(/\s/g, "")).toBe("123");
  expect(screen.getByRole("button", { name: "Revert to revision 3f9a2c1" })).toBeTruthy();
  expect(saveButton().disabled).toBe(true);
  expect(source.writes).toHaveLength(0);
});

test("typing is debounced into one check and a refused line blocks Save until a clean check", async () => {
  const source = await mount();
  change("[loop]\nworkers = \n");
  change(INVALID);
  expect(source.checks).toHaveLength(0);
  expect(saveButton().disabled).toBe(true);
  await checked();
  expect(source.checks).toHaveLength(1);
  expect(JSON.parse(String(source.checks[0]!.body))).toEqual({ text: INVALID, check: true });
  expect(screen.getByText("1 error blocks saving")).toBeTruthy();
  expect(screen.getByText("line 2")).toBeTruthy();
  expect(screen.getByText(SENTENCE)).toBeTruthy();
  expect(editor().parentElement!.querySelector('[data-line="2"]')!.className).toContain("text-bad");
  expect(screen.getByRole("link", { name: /Raw TOML/ }).textContent).toBe("Raw TOML1");
  expect(saveButton().disabled).toBe(true);
  change(CLEAN);
  expect(saveButton().disabled).toBe(true);
  expect(screen.getByRole("link", { name: /Raw TOML/ }).textContent).toBe("Raw TOML1");
  await checked();
  expect(screen.getByText("no errors · ready to save")).toBeTruthy();
  expect(saveButton().disabled).toBe(false);
  expect(screen.getByRole("link", { name: "Raw TOML" })).toBeTruthy();
});

test("Revert restores the file and a confirmed raw save sends its full text with the read revision", async () => {
  const source = await mount();
  change(CLEAN);
  fireEvent.click(screen.getByRole("button", { name: "Revert to revision 3f9a2c1" }));
  expect(editor().value).toBe(ORIGINAL);
  expect(saveButton().disabled).toBe(true);
  change(CLEAN);
  await checked();
  fireEvent.click(saveButton());
  const confirm = within(screen.getByRole("dialog", { name: "Save config.toml?" }));
  expect(confirm.getByText("− 2 workers = 2")).toBeTruthy();
  expect(confirm.getByText("+ 2 workers = 3")).toBeTruthy();
  expect(source.writes).toHaveLength(0);
  fireEvent.click(confirm.getByRole("button", { name: "Save" }));
  await act(settle);
  await act(settle);
  expect(source.writes).toHaveLength(1);
  expect(JSON.parse(String(source.writes[0]!.body))).toEqual({ text: CLEAN });
  expect(new Headers(source.writes[0]!.headers).get("If-Match")).toBe(REVISION);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(editor().value).toBe(CLEAN);
  expect(saveButton().disabled).toBe(true);
});

test("field and raw drafts block each other's controls across tabs until discarded or reverted", async () => {
  await mount({ tab: "general" });
  fireEvent.click(screen.getByRole("button", { name: "One more worker" }));
  await tab("Raw TOML");
  expect(screen.getByText("Save or discard the 1 field change before editing the raw file.")).toBeTruthy();
  expect(editor().readOnly).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(editor().readOnly).toBe(false);
  change(CLEAN);
  await tab("General");
  expect(screen.getByText("Save or revert the raw edit first.")).toBeTruthy();
  expect((screen.getByRole("button", { name: "One more worker" }).closest("fieldset") as HTMLFieldSetElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Human" }).closest("fieldset") as HTMLFieldSetElement).disabled).toBe(true);
  await tab("Raw TOML");
  expect(editor().value).toBe(CLEAN);
  fireEvent.click(screen.getByRole("button", { name: "Revert to revision 3f9a2c1" }));
  await tab("General");
  expect((screen.getByRole("button", { name: "One more worker" }) as HTMLButtonElement).disabled).toBe(false);
});

test("a late refusal for an older text cannot overwrite the clean current check", async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  await mount({ check: async text => text === INVALID ? pending : Response.json({ ok: true }) });
  change(INVALID);
  await checked();
  change(CLEAN);
  await checked();
  expect(saveButton().disabled).toBe(false);
  await act(async () => { finish(Response.json(REFUSAL, { status: 400 })); await settle(); });
  expect(screen.getByText("no errors · ready to save")).toBeTruthy();
  expect(saveButton().disabled).toBe(false);
  expect(screen.queryByText("line 2")).toBeNull();
});

test("a save-time refusal closes the confirm and marks the line while preserving the raw edit", async () => {
  await mount({ saveRefused: true });
  change(CLEAN);
  await checked();
  fireEvent.click(saveButton());
  fireEvent.click(within(screen.getByRole("dialog", { name: "Save config.toml?" })).getByRole("button", { name: "Save" }));
  await act(settle);
  await act(settle);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(editor().value).toBe(CLEAN);
  expect(screen.getByText("line 2")).toBeTruthy();
  expect(editor().parentElement!.querySelector('[data-line="2"]')!.className).toContain("text-bad");
  expect(saveButton().disabled).toBe(true);
});


test("line diff keeps shared lines and numbers removals in the old file and insertions in the new file", () => {
  expect(lineDiff("[loop]\nworkers = 2\nkeep = true\nobsolete = true\n", "# settings\n[loop]\nworkers = 3\nkeep = true\n")).toEqual([
    { kind: "added", number: 1, text: "# settings" },
    { kind: "removed", number: 2, text: "workers = 2" },
    { kind: "added", number: 3, text: "workers = 3" },
    { kind: "removed", number: 4, text: "obsolete = true" },
  ]);
});

test("raw confirmation caps changed lines at forty and counts the remainder", async () => {
  await mount();
  change(ORIGINAL + Array.from({ length: 46 }, (_, index) => `# line ${index + 1}`).join("\n") + "\n");
  await checked();
  fireEvent.click(saveButton());
  const confirm = within(screen.getByRole("dialog", { name: "Save config.toml?" }));
  expect(confirm.getByText("and 6 more lines")).toBeTruthy();
  expect(confirm.getByText("+ 42 # line 40")).toBeTruthy();
  expect(confirm.queryByText(/# line 41/)).toBeNull();
});
