import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { Findings, FindingsRun, FollowUpRow, RunDetail, ScoutRoutes, Scouts, TicketDefect, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { ACTIONS_OFF_LINE } from "../../src/next/components/CardActions";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const servedFindings = (await Bun.file(new URL("findings.json", SERVE)).json()) as Findings;
const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const servedRoutes = (await Bun.file(new URL("scout-routes.json", SERVE)).json()) as ScoutRoutes;

const runDetail = (id: number, ticket: string, phase: string, outcome: string | null): RunDetail => ({
  ...servedRun,
  run: { ...servedRun.run, id, ticket, title: `${ticket} title`, phase, outcome, pr_url: null, started_ms: NOW - 30 * MINUTE },
});
const runs: Record<string, RunDetail> = {
  "12": runDetail(12, "DEMO-7", "failed", "failed"),
  "13": runDetail(13, "DEMO-7", "done", "merged"),
  "14": runDetail(14, "DEMO-8", "working", null),
};

const ticket: TicketDetail = { ...servedTicket, ticket: "DEMO-7", title: "Save keeps the title", status: "ready", run: null, runs: [] };

const AT = NOW - 60 * MINUTE;
const servedRound = servedFindings.runs[0]!.rounds[0]!;
const findingAt = (message: string, path: string, line: number) => ({ ...servedRound.findings[0]!, severity: "p1", message, path, line });
const findingsRun = (id: number, ticketKey: string, outcome: string | null, message: string): FindingsRun => ({
  ...servedFindings.runs[0]!,
  run: id,
  ticket: ticketKey,
  phase: outcome === "merged" ? "done" : "failed",
  outcome,
  rounds: [{ ...servedRound, round: 1, started_ms: AT, ended_ms: AT, verdict: "changes_requested", findings: [findingAt(message, "app/save.py", 88)] }],
  ledger: [],
  advisories: [],
});
const followUp: FollowUpRow = { ...servedFindings.follow_ups[0]!, run: 12, ticket: "DEMO-7", text: "a later cache for saves", path: null, line: null, state: "pending" };
const defect: TicketDefect = {
  ticket: "DEMO-9", run: 15, kind: "contradiction", criteria: ["C2"], reason: "the criterion contradicts the verify", at: AT, revision: 2, revised_ms: AT, requeue_note: null,
};
const findings: Findings = {
  ...servedFindings,
  since: 0,
  runs: [findingsRun(12, "DEMO-7", "failed", "save drops the title"), findingsRun(11, "DEMO-6", "merged", "load reads twice")],
  follow_ups: [followUp],
  ticket_defects: [defect],
};

const demoRoutes: ScoutRoutes = {
  ...servedRoutes,
  leads: [{ ...servedRoutes.leads[0]!, name: "opus" }],
  helpers: [{ ...servedRoutes.helpers[0]!, name: "luna" }],
  defaults: { ...servedRoutes.defaults, lead: "opus", helper: "luna", swarm: 2, minutes: 30, helper_turns: 4 },
};

/** DEMO serving runs 12 to 14, DEMO-7, the findings above and its scout
 *  routes, `actions` as given; run 14 is live. Every request is kept. */
function scoutThisFetch(actions = true) {
  const seen: { url: string; method: string; body: Record<string, unknown> | null }[] = [];
  const polls = fleet({ items: [], runs: [{ ...run(14, "DEMO-8"), phase: "working" }] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    seen.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    const id = /^.*\/runs\/(\d+)$/.exec(url)?.[1];
    if (url.startsWith(DEMO) && id != null && id in runs) return Response.json(runs[id]);
    if (url === `${DEMO}/tickets/DEMO-7`) return Response.json(ticket);
    if (url.startsWith(`${DEMO}/findings?`)) return Response.json(findings);
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: [] });
    if (url === `${DEMO}/scouts/routes`) return Response.json(demoRoutes);
    if (url === `${DEMO}/actions/scout`) return Response.json({ ok: true, detail: "scout 32 started" });
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions });
  };
  return { fetch, seen };
}

async function mount(hash: string, fetch: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  for (let round = 0; round < 6; round += 1) await act(settle);
};

const scoutButtons = (scope: HTMLElement = document.body) => within(scope).queryAllByRole("button", { name: "Scout" });
const form = () => screen.getByRole("form", { name: "New scout" });
const ask = () => within(form()).getByLabelText("Ask") as HTMLTextAreaElement;
const pressedKind = () =>
  within(within(form()).getByRole("group", { name: "Kind" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent);
const project = () => (within(form()).getByLabelText("Project") as HTMLSelectElement).selectedOptions[0]!.textContent;
const about = () => form().querySelector("[data-about]")?.textContent ?? null;
const findingRow = (title: string) =>
  [...document.querySelectorAll<HTMLElement>("[data-finding-row]")].find((row) => row.querySelector("[data-title]")!.textContent === title)!;
const unfold = async (row: HTMLElement) => click(row.querySelector("button[aria-expanded]")!);
const scoutPosts = (seen: ReturnType<typeof scoutThisFetch>["seen"]) => seen.filter((each) => each.method === "POST" && each.url === `${DEMO}/actions/scout`);
const searchKeys = () => [...new URLSearchParams(window.location.hash.split("?")[1] ?? "").keys()];

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a failed run's Scout opens the New scout form asking why it failed, about the run; merged and running runs have none", async () => {
  await mount("#/runs/demo/12", scoutThisFetch().fetch);
  await click(scoutButtons()[0]!);
  expect(window.location.hash).toStartWith("#/scouts?");
  expect(ask().value).toBe("Why did run 12 of DEMO-7 fail?");
  expect(document.activeElement).toBe(ask());
  expect(ask().selectionStart).toBe(ask().value.length);
  expect(pressedKind()).toEqual(["Bug"]);
  expect(project()).toBe("demo");
  expect(about()).toBe("About run 12");
  cleanup();

  for (const id of [13, 14]) {
    await mount(`#/runs/demo/${id}`, scoutThisFetch().fetch);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(runs[String(id)]!.run.title);
    expect(scoutButtons()).toHaveLength(0);
    cleanup();
  }
});

test("an open finding's Scout asks to check it with its path and line; a follow-up holds one, a settled finding none", async () => {
  await mount("#/findings", scoutThisFetch().fetch);
  await unfold(findingRow("load reads twice"));
  expect(scoutButtons(findingRow("load reads twice"))).toHaveLength(0);
  await unfold(findingRow("a later cache for saves"));
  expect(scoutButtons(findingRow("a later cache for saves"))).toHaveLength(1);
  const open = findingRow("save drops the title");
  await unfold(open);
  await click(scoutButtons(open)[0]!);
  expect(ask().value).toBe("Check this finding from DEMO-7: save drops the title (app/save.py:88)");
  expect(about()).toBe("About run 12");
});

test("a ticket defect's Scout asks a research question about the ticket's wording, about the defect", async () => {
  await mount("#/findings", scoutThisFetch().fetch);
  const row = document.querySelector("[data-defect-row]") as HTMLElement;
  await click(scoutButtons(row)[0]!);
  expect(ask().value).toBe("What in DEMO-9's wording stopped run 15, and how should it read?");
  expect(pressedKind()).toEqual(["Research"]);
  expect(about()).toBe("About the defect in run 15");
});

test("a ticket's Scout opens a research form led by its key, about the ticket", async () => {
  await mount("#/tickets/demo/DEMO-7", scoutThisFetch().fetch);
  await click(scoutButtons(document.querySelector("[data-ticket-actions]") as HTMLElement)[0]!);
  expect(ask().value).toBe("DEMO-7: ");
  expect(pressedKind()).toEqual(["Research"]);
  expect(about()).toBe("About DEMO-7");
});

test("launching from a run posts its subject once, closes the form and drops the prefill from the search", async () => {
  const daemon = scoutThisFetch();
  await mount("#/runs/demo/12", daemon.fetch);
  await click(scoutButtons()[0]!);
  await click(within(form()).getByRole("button", { name: "Scout" }));
  const posts = scoutPosts(daemon.seen);
  expect(posts).toHaveLength(1);
  expect(posts[0]!.body).toEqual({ ask: "Why did run 12 of DEMO-7 fail?", kind: "bug", lead: "opus", helper: "luna", swarm: 2, minutes: 30, subject: "run:12" });
  expect(screen.queryByRole("form", { name: "New scout" })).toBeNull();
  expect(document.querySelector("[data-toast]")!.textContent).toBe("Scout 32 started.");
  expect(searchKeys().filter((key) => ["new", "about", "kind", "ask"].includes(key))).toEqual([]);
});

test("removing the About line launches with no subject", async () => {
  const daemon = scoutThisFetch();
  await mount("#/runs/demo/12", daemon.fetch);
  await click(scoutButtons()[0]!);
  await click(within(form()).getByRole("button", { name: "Remove run 12" }));
  expect(about()).toBeNull();
  await click(within(form()).getByRole("button", { name: "Scout" }));
  const posts = scoutPosts(daemon.seen);
  expect(posts).toHaveLength(1);
  expect(Object.keys(posts[0]!.body!)).not.toContain("subject");
});

test("a link names a pull request subject and kind; a project nobody offers opens the blank form's project with no subject", async () => {
  await mount("#/scouts?new=demo&about=pr:https://github.com/acme/demo/pull/7&kind=recon&ask=Map%20it", scoutThisFetch().fetch);
  expect(pressedKind()).toEqual(["Recon"]);
  expect(ask().value).toBe("Map it");
  expect(about()).toBe("About PR #7");
  cleanup();

  await mount("#/scouts?new=nope&about=run:12", scoutThisFetch().fetch);
  expect(project()).toBe("demo");
  expect(pressedKind()).toEqual(["Research"]);
  expect(about()).toBeNull();
});

test("with actions off, the Run page's and the Ticket page's Scout are disabled with the actions-off title", async () => {
  await mount("#/runs/demo/12", scoutThisFetch(false).fetch);
  const onRun = scoutButtons()[0]! as HTMLButtonElement;
  expect(onRun.disabled).toBe(true);
  expect(onRun.title).toBe(ACTIONS_OFF_LINE);
  cleanup();

  await mount("#/tickets/demo/DEMO-7", scoutThisFetch(false).fetch);
  const onTicket = scoutButtons(document.querySelector("[data-ticket-actions]") as HTMLElement)[0]! as HTMLButtonElement;
  expect(onTicket.disabled).toBe(true);
  expect(onTicket.title).toBe(ACTIONS_OFF);
});
