import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { Holds, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, NO_ATTENTION, settle, peersFetch } from "../harness";

const BASE = "http://writer-host:7710";
const statusFixture = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const holdsFixture = await Bun.file(new URL("../../../tests/fixtures/serve/holds.json", import.meta.url)).json() as Holds;
const stamp = (day: number, hour: number, minute: number) => new Date(2026, 9, day, hour, minute).getTime();
const history: Holds["rows"] = [
  { id: 2, at: stamp(9, 18, 2), action: "release_hold", note: "Schema bump is out; back to normal.", actor: "maintainer" },
  { id: 1, at: stamp(9, 13, 40), action: "hold", note: "Holding for the schema bump.", actor: "supervisor" },
];

async function mount(options: { status?: Partial<Status>; rows?: Holds["rows"]; missing?: boolean; post?: Fetch } = {}) {
  window.location.hash = "#/settings/hold?project=demo";
  const status: Status = { ...statusFixture, runs: [], project: "/srv/demo", workers: 2, actions: true, admission: "enabled", ...options.status };
  const poll = peersFetch(BASE, { [BASE]: { status, attention: NO_ATTENTION } });
  const requests: { url: string; method: string | undefined; body: unknown }[] = [];
  const fetch: Fetch = async (url, init) => {
    if (url.endsWith("/holds")) return options.missing ? new Response("not found", { status: 404 }) : Response.json({ ...holdsFixture, rows: options.rows ?? history });
    if (url.includes("/actions/")) {
      requests.push({ url, method: init?.method, body: JSON.parse(String(init?.body)) });
      return options.post ? options.post(url, init) : Response.json({ ok: true, detail: "done" });
    }
    return poll(url, init);
  };
  const client = consoleQueryClient();
  const view = render(<QueryClientProvider client={client}><NextConsole base={BASE} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return { ...view, requests };
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${BASE}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("the Hold settings route shows admission, worker capacity and newest-first history with actor names", async () => {
  const view = await mount();
  expect(screen.getAllByText("Taking work").length).toBeGreaterThan(0);
  expect(screen.getByText("Claims Ready tickets as its 2 workers free up.")).toBeTruthy();
  expect(screen.getByRole("group", { name: "Admission" })).toBeTruthy();
  expect(view.container.textContent).toMatch(/History.*Oct 9 18:02.*released.*Schema bump is out; back to normal\..*· you.*Oct 9 13:40.*held.*Holding for the schema bump\..*· the supervisor/);
  expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
});

for (const mode of ["Hold", "Release"] as const) {
  const releasing = mode === "Release";
  const open = releasing ? "Release" : "Held";
  const placeholder = releasing ? "Why it should take work again" : "Why it should stop taking work";
  const note = releasing ? "Back to normal" : "Schema bump";
  const status: Partial<Status> = { admission: releasing ? "held" : "enabled" };

  test(`${mode} requires a reason and posts it to the daemon before announcing success`, async () => {
    let finish!: (response: Response) => void;
    const view = await mount({ status, post: () => new Promise((resolve) => { finish = resolve; }) });
    fireEvent.click(screen.getByRole("button", { name: open }));
    expect((screen.getByRole("button", { name: `${mode} demo` }) as HTMLButtonElement).disabled).toBe(true);
    const reason = screen.getByPlaceholderText(placeholder);
    fireEvent.change(reason, { target: { value: "   " } });
    expect((screen.getByRole("button", { name: `${mode} demo` }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(reason, { target: { value: note } });
    fireEvent.click(screen.getByRole("button", { name: `${mode} demo` }));
    await act(settle);
    expect(view.requests).toEqual([{ url: `${BASE}/actions/${releasing ? "release-hold" : "hold"}`, method: "POST", body: { note } }]);
    expect((screen.getByRole("button", { name: releasing ? "Releasing…" : "Holding…" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { finish(Response.json({ ok: true, detail: "done" })); await settle(); });
    expect(screen.getByText(releasing ? "Released demo. Claims resume." : "Held demo. Nothing new is claimed.")).toBeTruthy();
  });

  test(`${mode} retains the reason and exposes a daemon refusal as an alert`, async () => {
    await mount({ status, post: async () => Response.json({ ok: false, detail: "project /x already held: y" }) });
    fireEvent.click(screen.getByRole("button", { name: open }));
    fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value: note } });
    fireEvent.click(screen.getByRole("button", { name: `${mode} demo` }));
    await act(settle);
    expect(screen.getByRole("alert").textContent).toContain("project /x already held: y");
    expect((screen.getByPlaceholderText(placeholder) as HTMLTextAreaElement).value).toBe(note);
  });
}

test("held admission names the latest hold time and reason and offers only Release", async () => {
  await mount({ status: { admission: "held", hold_note: "Waiting on the migration" }, rows: [
    { id: 3, at: stamp(14, 9, 12), action: "hold", note: "Waiting on the migration", actor: "operator" },
    ...history,
  ] });
  expect(screen.getByText("Held")).toBeTruthy();
  expect(screen.getByText("Held since Oct 14 09:12. Runs in flight finish; nothing new is claimed.")).toBeTruthy();
  expect(screen.getAllByText(/Waiting on the migration/).length).toBeGreaterThan(0);
  expect(screen.queryByRole("group", { name: "Admission" })).toBeNull();
  expect(screen.getByRole("button", { name: "Release" })).toBeTruthy();
  expect(screen.getByText("· the operator seat")).toBeTruthy();
});

test("empty or unavailable history stays usable and actions-off disables hold and release", async () => {
  for (const missing of [false, true]) {
    for (const admission of ["enabled", "held"] as const) {
      await mount({ rows: [], missing, status: { admission, actions: false } });
      expect(screen.getByText("Never held. It has taken work since it was added.")).toBeTruthy();
      const button = screen.getByRole("button", { name: admission === "enabled" ? "Held" : "Release" }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      expect(button.title).toBe(ACTIONS_OFF);
      cleanup();
    }
  }
});

test("stopped admission names its disable time and note without hold or release controls", async () => {
  await mount({ status: { admission: "disabled", hold_note: "Maintenance" }, rows: [
    { id: 3, at: stamp(9, 13, 40), action: "disable", note: "Maintenance", actor: null },
  ] });
  expect(screen.getByText("Stopped")).toBeTruthy();
  expect(screen.getByText("Stopped since Oct 9 13:40: its loop ends after its live runs.")).toBeTruthy();
  expect(screen.getByText("stopped")).toBeTruthy();
  expect(screen.queryByRole("group", { name: "Admission" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
});
