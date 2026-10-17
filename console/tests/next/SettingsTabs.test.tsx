import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { ConfigValues } from "../../src/lib/config";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, NO_ATTENTION, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const reviewValues = { loop: { review_rounds: 2, review_rounds_max: 4 }, review: { adversary: true } };

async function mount(tab: string, values: ConfigValues, options: { status?: Partial<Status>; probe?: { ok: boolean; output: string[] } } = {}) {
  const status: Status = { ...working, project: "/srv/demo", host: "writer-host", runs: [], config_edit: true, ...options.status };
  const poll = peersFetch(ORIGIN, { [ORIGIN]: { status, attention: { ...NO_ATTENTION, project: status.project } } });
  const writes: unknown[] = [];
  const fetch: Fetch = async (url, init) => {
    if (!url.endsWith("/config")) return poll(url, init);
    if (init?.method === "PUT") {
      writes.push(JSON.parse(String(init.body)));
      return Response.json({ revision: "b".repeat(64), applies: "next loop start", probe: options.probe });
    }
    return Response.json({ text: "", values, path: "/srv/demo/config.toml", applies: "next loop start", revision: "a".repeat(64) });
  };
  window.location.hash = `#/settings/${tab}?project=demo`;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={ORIGIN} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return writes;
}

const row = (name: string) => within(screen.getByRole("group", { name }));
const reviewButton = () => screen.getByRole("button", { name: "Review and save" }) as HTMLButtonElement;
async function save() {
  fireEvent.click(reviewButton());
  fireEvent.click(within(screen.getByRole("dialog", { name: "Save these changes?" })).getByRole("button", { name: "Save" }));
  await act(settle);
  await act(settle);
}
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("Review binds the four file keys and shows the absent review mode as Single by default", async () => {
  await mount("review", reviewValues);
  for (const [label, key] of [["Review rounds", "loop.review_rounds"], ["Round cap", "loop.review_rounds_max"], ["Adversary", "review.adversary"], ["Review mode", "agents.review_mode"]]) {
    expect(row(label!).getByText(key!)).toBeTruthy();
  }
  expect(row("Review rounds").getByRole("status").textContent).toBe("2");
  expect(row("Round cap").getByRole("status").textContent).toBe("4");
  expect(row("Adversary").getByRole("switch").textContent).toBe("On");
  expect(row("Adversary").getByRole("switch").getAttribute("aria-checked")).toBe("true");
  expect(row("Review mode").getByRole("button", { name: "Single" }).getAttribute("aria-pressed")).toBe("true");
  expect(row("Review mode").getByText("default")).toBeTruthy();
});

test("Review blocks a cap below its rounds, allows equality and keeps the cap at least one", async () => {
  await mount("review", reviewValues);
  for (let count = 0; count < 3; count++) fireEvent.click(row("Round cap").getAllByRole("button")[0]!);
  expect(row("Review rounds").getByRole("alert").textContent).toBe("Above the round cap of 1. Raise the cap or lower this.");
  expect(screen.getByRole("region", { name: "Unsaved changes" }).textContent).toContain("· fix the error first");
  expect(reviewButton().disabled).toBe(true);
  expect((row("Round cap").getAllByRole("button")[0] as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(row("Round cap").getAllByRole("button")[1]!);
  expect(row("Review rounds").queryByRole("alert")).toBeNull();
  expect(reviewButton().disabled).toBe(false);
});

test("Agents show live, fallback and down routes beside commands and reject an emptied command", async () => {
  await mount("agents", { agents: { implementer: "claude --model opus -p" } }, { status: {
    route_labels: { ...working.route_labels, implementer: "claude opus" },
    active_routes: { implementer: { command: "claude --model opus -p" }, reviewer: { command: "codex exec", fallback: "codex exec" }, adjudicator: { command: null, down: true } },
  } });
  const implementer = row("Implementer").getByRole("textbox") as HTMLInputElement;
  expect(implementer.value).toBe("claude --model opus -p");
  expect(row("Implementer").getByText("runs claude opus").className).toContain("text-live");
  const reviewer = row("Reviewer").getByRole("textbox") as HTMLInputElement;
  expect(reviewer.value).toBe("");
  expect(reviewer.placeholder).toBe("the default route");
  expect(row("Reviewer").getByText("default")).toBeTruthy();
  expect(row("Reviewer").getByText("on its fallback codex exec").className).toContain("text-ask");
  expect(row("Adjudicator").getByText("down").className).toContain("text-bad");
  fireEvent.change(implementer, { target: { value: "" } });
  expect(row("Implementer").getByRole("alert").textContent).toBe("A seat needs a command. Remove the key in Raw TOML to use the default.");
  expect(reviewButton().disabled).toBe(true);
  fireEvent.change(implementer, { target: { value: "codex exec" } });
  expect(row("Implementer").queryByRole("alert")).toBeNull();
  expect(reviewButton().disabled).toBe(false);
});

test("Capture enables Video only with a command and saves UI paths as individual globs", async () => {
  const writes = await mount("capture", { merge: { ui_capture: "", ui_paths: ["console/**", "assets/**"] } });
  const video = row("Video").getByRole("switch") as HTMLButtonElement;
  expect(video.disabled).toBe(true);
  expect(video.getAttribute("aria-checked")).toBe("false");
  const paths = row("UI paths").getByRole("textbox") as HTMLTextAreaElement;
  expect(paths.value).toBe("console/**\nassets/**");
  fireEvent.change(row("UI capture command").getByRole("textbox"), { target: { value: "bun run capture" } });
  expect(video.disabled).toBe(false);
  fireEvent.click(video);
  fireEvent.change(paths, { target: { value: "console/src/**\nimages/**\n\n" } });
  await save();
  expect(writes).toEqual([{ patch: { "merge.ui_capture": "bun run capture", "merge.ui_capture_video": true, "merge.ui_paths": ["console/src/**", "images/**"] } }]);
});

test.each([
  [{ ok: false, output: ["starting probe", "harness: refused"] }, "Implementer probe failed: harness: refused"],
  [{ ok: true, output: ["ready"] }, "Implementer probe passed."],
] as const)("saving an implementer command reports the probe verdict %j", async (probe, message) => {
  const writes = await mount("agents", { agents: { implementer: "claude --model opus -p" } }, { probe: { ...probe, output: [...probe.output] } });
  fireEvent.change(row("Implementer").getByRole("textbox"), { target: { value: "harness --fast" } });
  await save();
  expect(writes).toEqual([{ patch: { "agents.implementer": "harness --fast" } }]);
  const banner = screen.getAllByRole("status").find((item) => item.textContent?.includes("Saved config.toml at revision"));
  expect(banner?.textContent).toContain(message);
  if (!probe.ok) expect(within(banner!).getByText(message).className).toContain("text-bad");
});

test("UI paths retain an entered newline so another glob can be typed on its own line", async () => {
  const writes = await mount("capture", { merge: { ui_capture: "bun run capture", ui_paths: ["console/**"] } });
  const paths = row("UI paths").getByRole("textbox") as HTMLTextAreaElement;
  fireEvent.change(paths, { target: { value: `${paths.value}\n` } });
  expect(paths.value).toBe("console/**\n");
  fireEvent.change(paths, { target: { value: `${paths.value}assets/**` } });
  await save();
  expect(writes).toEqual([{ patch: { "merge.ui_paths": ["console/**", "assets/**"] } }]);
});
