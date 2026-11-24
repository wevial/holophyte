import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, Status, StatusRun } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import bypassable from "../../../tests/fixtures/serve/run-merge-bypassable.json";
import ready from "../../../tests/fixtures/serve/run-merge-ready.json";
import ticketDetail from "../../../tests/fixtures/serve/ticket.json";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle, type StubDaemon } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NOW = new Date(2026, 9, 15, 16, 30).getTime();

type Item = Attention["items"][number];
const park = (run: number, parkKind: string, ageMs: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run, ticket: `DEMO-${run}`, ticket_url: null, title: `Ticket ${run}`,
  park_kind: parkKind, reason: "PR open", asked_ms: NOW - ageMs,
  pr_url: `https://github.com/example/demo/pull/${run + 33}`, pr: { title: null, state: "ready", number: run + 33, ...pr },
} as Item);
const onCi = park(8, "ci", 3 * HOUR, { number: 41, checks: "pending", review: null, threads: 0, open_threads: 0 });
const readyPark = park(9, "pull_request", 2 * HOUR, { number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0 });
const threadPark = park(10, "pull_request", HOUR, { number: 99, checks: "success", review: "approved", threads: 1, open_threads: 1 });
const draftPark = park(12, "pull_request", HOUR, { number: 45, state: "draft", checks: "success", review: null, threads: 0, open_threads: 0 });
const PR_42 = "https://github.com/example/demo/pull/42";
const WHAT = "## What / Why / How\n\n**What:** Adds a slot that takes pages.\n\n**Why:** Paging.\n";

interface Daemon {
  items: Item[];
  runs?: StatusRun[];
  actions?: boolean;
  readyRuns?: number[];
  /** A run's `/runs/N/merge` answer, or the status it fails with, over `readyRuns`. */
  answers?: Record<number, object | number>;
  ticketBody?: string;
  sendBack?: { ok: boolean; detail: string };
  markReady?: { ok: boolean; detail: string };
  merge?: { ok: boolean; detail: string };
  /** What `demo` answers once mark-ready is posted; its `/attention` waits on `answer`. */
  woken?: { items: Item[]; queued: Status["queued"]; answer: Promise<void> };
  /** How far the daemons' clock runs behind the console's. */
  behindMs?: number;
  /** The second daemon's parks, how far its clock runs behind, and its ready runs. */
  writer?: { items: Item[]; behindMs?: number; readyRuns?: number[] } | "down";
  hash?: string;
}

let client: QueryClient | null = null;
/** `demo`'s `/attention` requests made after the mark-ready post. */
let readsAfterPost = 0;
/** The daemons the fleet poll reads, by base; a test may swap one for a `down` stub. */
let daemons: Record<string, StubDaemon> = {};
let clock = { now: NOW };

async function mount({ items, runs = [], actions = false, readyRuns = [9], answers = {}, ticketBody = "", sendBack = { ok: true, detail: "sent back" }, markReady = { ok: true, detail: "marked ready" }, merge = { ok: true, detail: "approved" }, woken, behindMs = 0, writer, hash = "#/" }: Daemon) {
  const statusOf = (project: string, own: StatusRun[] = [], behind = behindMs): Status => ({ ...served, project, now: NOW - behind, runs: own, queued: [], actions });
  const attention = (project: string, list: Item[], behind = behindMs) => ({ level: list.length ? "attention" : "none", now: NOW - behind, project, items: list });
  const [first, second] = ["/srv/dev/demo", "/srv/dev/writer"];
  const answering = writer === "down" ? undefined : writer;
  const writerBehind = answering?.behindMs ?? behindMs;
  daemons = {
    [DEMO]: { status: statusOf(first, runs), attention: attention(first, items) },
    [WRITER]: writer === "down" ? { down: new Error("connection refused") } : { status: statusOf(second, [], writerBehind), attention: attention(second, answering?.items ?? [], writerBehind) },
  };
  const fleet = peersFetch(DEMO, daemons);
  const { seen, fetchImpl } = fakeFetch({});
  let posted = false;
  readsAfterPost = 0;
  const routed: typeof fetchImpl = async (url, init) => {
    if (posted && url === `${DEMO}/attention`) readsAfterPost += 1;
    if (posted && woken && url === `${DEMO}/attention`) {
      await woken.answer;
      return Response.json(attention(first, woken.items));
    }
    if (posted && woken && url === `${DEMO}/status`) return Response.json({ ...statusOf(first, runs), queued: woken.queued });
    const readiness = url.match(/\/runs\/(\d+)\/merge$/);
    if (readiness) {
      const onWriter = url.startsWith(`${WRITER}/`);
      const answer = onWriter ? undefined : answers[Number(readiness[1])];
      if (typeof answer === "number") return new Response("readiness failed", { status: answer });
      const readyHere = onWriter ? answering?.readyRuns ?? [] : readyRuns;
      return Response.json(answer ?? { ...ready, run: Number(readiness[1]), ready: readyHere.includes(Number(readiness[1])) });
    }
    const ticket = url.match(/\/tickets\/([^/?]+)$/);
    if (ticket) {
      await fetchImpl(url, init);
      return Response.json({ ...ticketDetail, ticket: decodeURIComponent(ticket[1]!), body: ticketBody });
    }
    if (url.endsWith("/actions/send-back")) {
      await fetchImpl(url, init);
      return Response.json(sendBack);
    }
    if (url.endsWith("/actions/merge")) {
      await fetchImpl(url, init);
      return Response.json(merge);
    }
    if (url.endsWith("/actions/mark-ready")) {
      await fetchImpl(url, init);
      posted = true;
      return Response.json(markReady);
    }
    return fleet(url, init);
  };
  window.location.hash = hash;
  const fake = fakeDeps(routed);
  fake.clock.now = NOW;
  clock = fake.clock;
  client = consoleQueryClient();
  render(
    <QueryClientProvider client={client}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return seen;
}

function unmount() {
  cleanup();
  client?.clear();
}

const section = () => screen.getByRole("region", { name: "Waiting" });
const rows = () => [...section().querySelectorAll<HTMLElement>("[data-pr-row]")];
const line = (row: HTMLElement) => row.querySelector<HTMLElement>("[data-row]")!;
/** Each row's state glyph label and its age, top to bottom. */
const waits = () => rows().map((row) => [
  within(line(row)).getByRole("img").getAttribute("aria-label"),
  row.querySelector("[data-age]")?.textContent ?? "",
]);
/** A row's title button, by its run on `demo` or by its ticket key. */
const rowButton = (run: number | string) => within(section()).getByRole("button", { name: new RegExp(`^${typeof run === "number" ? `DEMO-${run}` : run} `) });
const keys = () => rows().map((row) => line(row).textContent!.match(/^[A-Z]+-\d+/)![0]);
const repoll = async () => {
  await act(async () => {
    await client!.refetchQueries();
    await settle();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
};

async function openRow(run: number | string) {
  await act(async () => {
    fireEvent.click(rowButton(run));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  unmount();
  client = null;
  window.location.hash = "";
});

test("Waiting lists every pull request park, a ci park and a ready one too, oldest first under its count, and says when none waits", async () => {
  await mount({ items: [threadPark, readyPark, onCi] });
  expect(section().querySelector("[data-count]")!.textContent).toBe("3");
  expect(rows().map((row) => within(row).getByRole("link", { name: /^#/ }).textContent)).toEqual(["#41", "#42", "#43"]);
  expect(waits()).toEqual([["On CI", "3h"], ["Your review", "2h"], ["Your review", "1h"]]);
  unmount();

  await mount({ items: [] });
  expect(rows()).toEqual([]);
  expect(section().textContent).toContain("Nothing waits on a pull request.");
});

test("a row's age counts on its daemon's clock, not the console's", async () => {
  await mount({ items: [{ ...readyPark, asked_ms: NOW - 5 * MINUTE - 2 * MINUTE }], behindMs: 5 * MINUTE });
  expect(waits()).toEqual([["Your review", "2m"]]);
});

test("a draft held for review labels its glyph Draft and its age with no second line, while a ready row reads Your review then ready to merge", async () => {
  await mount({ items: [park(12, "pull_request", 30 * MINUTE, {
    state: "draft", title: "Improve the overview", checks: "success", review: "review_required", threads: 0, open_threads: 0,
  }), readyPark] });
  expect(waits()).toEqual([["Your review", "2h"], ["Draft · 30m", "30m"]]);
  expect(rowButton(12).getAttribute("aria-label")).toBe("DEMO-12 Improve the overview, Draft · 30m");
  expect(rowButton(9).getAttribute("aria-label")).toBe("DEMO-9 Ticket 9, Your review, ready to merge · 2h");
});

test("a row names its pull request by the bare number in its URL", async () => {
  await mount({ items: [readyPark, threadPark] });
  expect(within(rows()[1]!).getByRole("link", { name: "#43" }).getAttribute("href")).toBe("https://github.com/example/demo/pull/43");
});

test("an opened row shows its ticket's What line and the links that move it, not the classic detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [], ticketBody: WHAT });
  await openRow(9);
  const row = rows()[0]!;
  expect(rowButton(9).getAttribute("aria-expanded")).toBe("true");
  expect(within(row).getByText("Adds a slot that takes pages.")).toBeTruthy();
  expect(within(row).getByRole("link", { name: "Open PR to review" }).getAttribute("href")).toBe(PR_42);
  expect(within(row).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/9");
  expect(within(row).queryByRole("region", { name: "Pull request facts" })).toBeNull();
  expect(row.textContent).not.toContain("Last factory activity");
  expect(within(row).queryByText(PR_42)).toBeNull();
  expect(seen.map((request) => request.url)).toEqual([`${DEMO}/tickets/DEMO-9`]);
});

test("Send back posts the typed note for the run, keeps it on a refusal, and is absent without actions", async () => {
  const row = () => rows()[0]!;
  const sendNote = async () => {
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Send back with a note" }));
      await settle();
    });
    fireEvent.change(within(row()).getByLabelText("Note for the factory"), { target: { value: "Rebase on main first" } });
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Send" }));
      await settle();
    });
  };
  const seen = await mount({ items: [readyPark], actions: true, readyRuns: [] });
  await openRow(9);
  await sendNote();
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/send-back`, body: { run: 9, note: "Rebase on main first" } }]);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Sent PR #42 back with your note.");
  unmount();

  await mount({ items: [readyPark], actions: true, readyRuns: [], sendBack: { ok: false, detail: "run 9 is not parked" } });
  await openRow(9);
  await sendNote();
  expect((within(row()).getByLabelText("Note for the factory") as HTMLTextAreaElement).value).toBe("Rebase on main first");
  expect(within(row()).getByRole("alert").textContent).toBe("run 9 is not parked");
  unmount();

  await mount({ items: [readyPark], actions: false, readyRuns: [] });
  await openRow(9);
  expect(within(row()).getByRole("link", { name: "Open run" })).toBeTruthy();
  expect(within(row()).queryByRole("button", { name: "Send back with a note" })).toBeNull();
});

test("Mark ready for review confirms, posts the run and toasts on a draft with actions; a refusal shows its detail", async () => {
  const row = () => rows()[0]!;
  const markReady = async () => {
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Mark ready for review" }));
      await settle();
    });
    const confirm = within(screen.getByRole("alertdialog", { name: "Mark #45 ready for review?" }));
    expect(confirm.getByText("Review bots and the full checks start on GitHub.")).toBeTruthy();
    await act(async () => {
      fireEvent.click(confirm.getByRole("button", { name: "Mark ready for review" }));
      await settle();
    });
  };
  const seen = await mount({ items: [draftPark], actions: true, readyRuns: [] });
  await openRow(12);
  const buttons = [...row().querySelectorAll("[data-pr-detail] a, [data-pr-detail] button")].map((node) => node.textContent);
  expect(buttons.slice(0, 2)).toEqual(["Open PR to review", "Mark ready for review"]);
  await markReady();
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/mark-ready`, body: { run: 12 } }]);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Marked #45 ready for review.");
  expect(screen.queryByRole("alertdialog")).toBeNull();
  unmount();

  await mount({ items: [draftPark], actions: true, readyRuns: [], markReady: { ok: false, detail: "run 12 is not a draft held for review" } });
  await openRow(12);
  await markReady();
  expect(within(screen.getByRole("alertdialog")).getByRole("alert").textContent).toBe("run 12 is not a draft held for review");
  expect(waits()).toEqual([["Draft · 1h", "1h"]]);
});

test("an ok Mark ready turns #45's Waiting row On CI at once, and the read after the post settles it off Waiting and into the queued count", async () => {
  let answer = () => {};
  const held = new Promise<void>((resolve) => (answer = resolve));
  const queued = [{ ...served.queued[0]!, ticket: "DEMO-12", pr_url: "https://github.com/example/demo/pull/45" }];
  await mount({ items: [draftPark], actions: true, readyRuns: [], woken: { items: [], queued, answer: held } });
  await openRow(12);
  await act(async () => {
    fireEvent.click(within(rows()[0]!).getByRole("button", { name: "Mark ready for review" }));
    await settle();
  });
  await act(async () => {
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Mark ready for review" }));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(readsAfterPost).toBe(1);
  expect(waits()).toEqual([["On CI", "1h"]]);
  expect(within(rows()[0]!).getByRole("link", { name: "#45" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "0 queued" })).toBeTruthy();

  await act(async () => {
    answer();
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(readsAfterPost).toBe(1);
  expect(rows()).toEqual([]);
  expect(screen.getByRole("img", { name: "1 queued" })).toBeTruthy();
});

test("Mark ready for review is absent on a ready pull request and on a daemon without actions", async () => {
  for (const [item, actions] of [[readyPark, true], [draftPark, false]] as const) {
    await mount({ items: [item], actions, readyRuns: [] });
    await openRow(item.run as number);
    expect(within(rows()[0]!).getByRole("link", { name: "Open PR to review" })).toBeTruthy();
    expect(within(rows()[0]!).queryByRole("button", { name: "Mark ready for review" })).toBeNull();
    unmount();
  }
});

const TOKEN = "tok-478";
const detailButton = (name: string) => within(rows()[0]!.querySelector<HTMLElement>("[data-pr-detail]")!).queryByRole("button", { name });
const pressMerge = async (name: string) => {
  await act(async () => {
    fireEvent.click(name === "Merge" ? detailButton("Merge")! : within(screen.getByRole("alertdialog")).getByRole("button", { name }));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
};

test("an opened ready-to-merge row leads with Merge, whose confirmed dialog posts the run with the bearer and toasts", async () => {
  storeToken(addressOf(DEMO), TOKEN);
  const seen = await mount({ items: [readyPark], actions: true });
  await openRow(9);
  expect(detailButton("Merge")!.dataset.variant).toBe("primary");
  expect(within(rows()[0]!).getByRole("link", { name: "Open PR to review" }).dataset.variant).toBe("quiet");
  await pressMerge("Merge");
  const dialog = screen.getByRole("alertdialog", { name: "Merge PR #42?" });
  const safe = within(dialog).getByRole("list", { name: "Why it is safe" }).querySelectorAll("li");
  expect([...safe].map((fact) => fact.textContent)).toEqual(ready.facts.map((fact) => fact.detail));
  expect(dialog.textContent).toContain("The console cannot undo a merge");
  fireEvent.keyDown(within(dialog).getByRole("button", { name: "Cancel" }), { key: "Escape" });
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(seen.filter((request) => request.method === "POST")).toEqual([]);
  await pressMerge("Merge");
  await pressMerge("Merge PR #42");
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body, authorization }) => ({ url, body, authorization }))).toEqual([
    { url: `${DEMO}/actions/merge`, body: { run: 9 }, authorization: `Bearer ${TOKEN}` },
  ]);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Approved PR #42. The loop merges it on its next claim.");
});

test("a refused merge keeps the row's dialog open with the daemon's reason and its confirm pressable again", async () => {
  const seen = await mount({ items: [readyPark], actions: true, merge: { ok: false, detail: "GitHub's review decision is REVIEW_REQUIRED" } });
  await openRow(9);
  await pressMerge("Merge");
  await pressMerge("Merge PR #42");
  const dialog = screen.getByRole("alertdialog", { name: "Merge PR #42?" });
  expect(within(dialog).getByRole("alert").textContent).toBe("GitHub's review decision is REVIEW_REQUIRED");
  const confirm = within(dialog).getByRole("button", { name: "Merge PR #42" });
  expect(confirm.hasAttribute("disabled")).toBe(false);
  expect(document.querySelector("[data-toast]")?.textContent ?? "").toBe("");
  await pressMerge("Merge PR #42");
  expect(seen.filter((request) => request.url.endsWith("/actions/merge"))).toHaveLength(2);
});

test("an opened row whose readiness answers not ready draws no Merge and keeps Open PR to review primary", async () => {
  await mount({ items: [readyPark], actions: true, readyRuns: [] });
  await openRow(9);
  expect(detailButton("Merge")).toBeNull();
  expect(within(rows()[0]!).getByRole("link", { name: "Open PR to review" }).dataset.variant).toBe("primary");
});

const notReady = (run: number, reason: string) => ({ ...bypassable, run, reason });
const bypassAnswer = { 9: notReady(9, "review_bypassable") };

test("a not-ready row labels its glyph and its button with its readiness reason, keeping its age and Your review for a reason without copy", async () => {
  const failing = park(10, "pull_request", 90 * MINUTE, { checks: "failure", review: "approved", threads: 0, open_threads: 0 });
  const conflict = park(11, "pull_request", 45 * MINUTE, { checks: "success", review: "approved", threads: 0, open_threads: 0 });
  const unapproved = park(13, "pull_request", 30 * MINUTE, { checks: "success", review: "review_required", threads: 0, open_threads: 0 });
  await mount({ items: [readyPark, failing, conflict, unapproved], answers: {
    ...bypassAnswer, 10: notReady(10, "checks_failing"), 11: notReady(11, "conflicting"), 13: notReady(13, "review_not_approved"),
  } });
  expect(waits()).toEqual([["Review required", "2h"], ["Checks failing", "1h"], ["Merge conflict with main", "45m"], ["Your review", "30m"]]);
  for (const [run, word] of [[9, "Review required"], [10, "Checks failing"], [11, "Merge conflict with main"], [13, "Your review"]] as const) {
    expect(rowButton(run).getAttribute("aria-label")).toContain(word);
  }
});

test("a row whose readiness read fails labels its glyph Your review and opens with no Merge of either kind", async () => {
  await mount({ items: [readyPark], actions: true, answers: { 9: 500 } });
  await openRow(9);
  expect(waits()).toEqual([["Your review", "2h"]]);
  expect(detailButton("Merge without review")).toBeNull();
  expect(detailButton("Merge")).toBeNull();
});

test("an opened bypassable row leads with Merge without review with actions on, and draws none with actions off", async () => {
  await mount({ items: [readyPark], actions: true, answers: bypassAnswer });
  await openRow(9);
  const detail = rows()[0]!.querySelector<HTMLElement>("[data-pr-detail]")!;
  const first = within(detail).getAllByRole("button")[0]!;
  expect([first.textContent, first.dataset.variant]).toEqual(["Merge without review", "primary"]);
  expect(within(detail).getByRole("link", { name: "Open PR to review" }).dataset.variant).toBe("quiet");
  expect(detailButton("Merge")).toBeNull();
  unmount();

  await mount({ items: [readyPark], actions: false, answers: bypassAnswer });
  await openRow(9);
  expect(detailButton("Merge without review")).toBeNull();
  expect(within(rows()[0]!).getByRole("link", { name: "Open PR to review" }).dataset.variant).toBe("primary");
});

const pressBypass = async () => {
  await act(async () => {
    fireEvent.click(detailButton("Merge without review")!);
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
};

test("Merge without review confirms over the readiness facts with the review fact flagged, and posts the bypass with the bearer", async () => {
  storeToken(addressOf(DEMO), TOKEN);
  const seen = await mount({ items: [readyPark], actions: true, answers: bypassAnswer });
  await openRow(9);
  await pressBypass();
  const dialog = screen.getByRole("alertdialog", { name: "Merge PR #42 without its review?" });
  const safe = [...within(dialog).getByRole("list", { name: "Why it is safe" }).querySelectorAll("li")];
  expect(safe.map((fact) => fact.textContent)).toEqual(bypassable.facts.map((fact) => fact.detail));
  const icons = safe.map((fact) => fact.querySelector("svg")!.getAttribute("class") ?? "");
  bypassable.facts.forEach((fact, index) => expect(icons[index]).toContain(fact.name === "review_approved" ? "text-ask-ink" : "text-live"));
  expect(dialog.textContent).toContain("The console cannot undo a merge");
  expect(within(dialog).getByRole("button", { name: "Merge PR #42" }).dataset.variant).toBe("danger-fill");
  fireEvent.keyDown(within(dialog).getByRole("button", { name: "Cancel" }), { key: "Escape" });
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(seen.filter((request) => request.method === "POST")).toEqual([]);
  await pressBypass();
  await pressMerge("Merge PR #42");
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body, authorization }) => ({ url, body, authorization }))).toEqual([
    { url: `${DEMO}/actions/merge`, body: { run: 9, bypass_review: true }, authorization: `Bearer ${TOKEN}` },
  ]);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Approved PR #42 without its review. The loop merges it on its next claim.");
});

test("a refused bypass merge keeps its dialog open with the daemon's detail and its confirm pressable", async () => {
  const detail = "the pull request's merge readiness is now checks_failing, not review_bypassable";
  await mount({ items: [readyPark], actions: true, answers: bypassAnswer, merge: { ok: false, detail } });
  await openRow(9);
  await pressBypass();
  await pressMerge("Merge PR #42");
  const dialog = screen.getByRole("alertdialog", { name: "Merge PR #42 without its review?" });
  expect(within(dialog).getByRole("alert").textContent).toBe(detail);
  expect(within(dialog).getByRole("button", { name: "Merge PR #42" }).hasAttribute("disabled")).toBe(false);
  expect(document.querySelector("[data-toast]")?.textContent ?? "").toBe("");
});

test("the PR number links to GitHub without toggling or fetching the row detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [] });
  const link = within(rows()[0]!).getByRole("link", { name: "#42" });
  const button = rowButton(9);
  expect(link.getAttribute("href")).toBe(PR_42);
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noreferrer");
  expect(link.closest("button")).toBeNull();
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(seen).toEqual([]);
  await act(async () => {
    fireEvent.click(button);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(seen.some(({ url }) => url === `${DEMO}/tickets/DEMO-9`)).toBe(true);
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
});

const SHOTS = ["01 split view", "02 custom slot", "03 saved document"].map((caption, index) => ({ caption, url: `https://media.example/${index + 1}.png` }));
const VIDEOS = [{ caption: "video-01.webm", url: "https://media.example/video-01.webm" }];
const TILE_NAMES = ["01 split view", "02 custom slot", "03 saved document", "Video: video-01.webm"];
const mediaPark = (media: object) => park(9, "pull_request", 2 * HOUR, {
  number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0, preview_url: null, ...media,
});
const tileNames = (row: HTMLElement) => [...row.querySelectorAll<HTMLElement>("[data-media-tile]")].map((tile) => tile.getAttribute("aria-label"));
const tile = (name: string) => within(rows()[0]!).getByRole("button", { name });
const dialog = () => screen.queryByRole("dialog");
const press = (key: string, shiftKey = false) => act(() => void fireEvent.keyDown(document.activeElement ?? document.body, { key, shiftKey }));

async function openMedia(name: string) {
  await mount({ items: [mediaPark({ screenshots: SHOTS, videos: VIDEOS })], readyRuns: [] });
  await openRow(9);
  await act(async () => {
    fireEvent.click(tile(name));
    await settle();
  });
}

test("a row reads its key before its title and its bare number after, and opening it marks only its row and shows Open Vercel preview", async () => {
  await mount({ items: [onCi, park(9, "pull_request", 2 * HOUR, {
    number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0,
    preview_url: "https://preview.example", screenshots: SHOTS,
  }), threadPark], readyRuns: [] });
  const demo9 = () => rows().find((row) => row.textContent!.includes("DEMO-9"))!;
  expect(within(demo9()).getByRole("link", { name: "#42" }).getAttribute("href")).toBe(PR_42);
  expect(line(demo9()).textContent).toBe("DEMO-9Ticket 9#422h");
  expect(rows().filter((row) => row.hasAttribute("data-open"))).toEqual([]);
  await openRow(9);
  expect(rows().filter((row) => row.hasAttribute("data-open"))).toEqual([demo9()]);
  expect(within(demo9()).getByRole("link", { name: "Open Vercel preview" }).getAttribute("href")).toBe("https://preview.example");
  expect(within(demo9()).queryByRole("link", { name: "Open preview" })).toBeNull();
});

test("an opened row draws a tile button per screenshot, then per video, and none when it has no media", async () => {
  await mount({ items: [mediaPark({ screenshots: SHOTS, videos: VIDEOS })], readyRuns: [] });
  await openRow(9);
  expect(tileNames(rows()[0]!)).toEqual(TILE_NAMES);
  for (const name of TILE_NAMES) expect(tile(name).tagName).toBe("BUTTON");
  expect(within(rows()[0]!.querySelector("[data-media]")!).queryAllByRole("link")).toEqual([]);
  expect(within(tile("02 custom slot")).getByRole("img").getAttribute("alt")).toBe("02 custom slot");
  unmount();

  await mount({ items: [mediaPark({ screenshots: SHOTS })], readyRuns: [] });
  await openRow(9);
  expect(tileNames(rows()[0]!)).toEqual(TILE_NAMES.slice(0, 3));
  unmount();

  await mount({ items: [mediaPark({ screenshots: [] })], readyRuns: [] });
  await openRow(9);
  expect(tileNames(rows()[0]!)).toEqual([]);
  expect(within(rows()[0]!).queryByRole("link", { name: "Open Vercel preview" })).toBeNull();
});

test("a tile opens its item in a lightbox that Left and Right step through screenshots then videos, stopping at the ends", async () => {
  await openMedia("02 custom slot");
  const shown = () => screen.getByRole("dialog", { name: "Screenshot 2 of 4" });
  expect(within(shown()).getByText("2 / 4")).toBeTruthy();
  expect(within(shown()).getByText("2.png")).toBeTruthy();
  const image = within(shown()).getByRole("img");
  expect(image.getAttribute("src")).toBe(SHOTS[1]!.url);
  expect(image.getAttribute("alt")).toBe("02 custom slot");
  expect(shown().querySelector("[data-media-caption]")!.textContent).toBe("02 custom slot");
  expect(image.compareDocumentPosition(shown().querySelector("[data-media-caption]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  await press("ArrowRight");
  await press("ArrowRight");
  const last = screen.getByRole("dialog", { name: "Video 4 of 4" });
  const video = last.querySelector("video")!;
  expect(video.getAttribute("src")).toBe(VIDEOS[0]!.url);
  expect(video.hasAttribute("controls")).toBe(true);
  expect(video.hasAttribute("autoplay")).toBe(false);
  expect(video.muted).toBe(false);
  video.focus();
  expect(fireEvent.keyDown(video, { key: "Tab" })).toBe(true);
  expect((within(last).getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
  await press("ArrowRight");
  expect(dialog()!.getAttribute("aria-label")).toBe("Video 4 of 4");

  await press("ArrowLeft");
  expect(dialog()!.getAttribute("aria-label")).toBe("Screenshot 3 of 4");
  await press("ArrowLeft");
  await press("ArrowLeft");
  await press("ArrowLeft");
  expect(dialog()!.getAttribute("aria-label")).toBe("Screenshot 1 of 4");
  expect((within(dialog()!).getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
});

async function waitInert(element: HTMLElement) {
  for (let attempt = 0; attempt < 25 && element.closest("[inert]") == null; attempt += 1) {
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 10)));
  }
  expect(element.closest("[inert]")).not.toBeNull();
}

test("the lightbox keeps focus inside, and Esc or Close returns it to the tile of the item shown last", async () => {
  await openMedia("02 custom slot");
  await press("ArrowRight");
  expect(dialog()!.getAttribute("aria-label")).toBe("Screenshot 3 of 4");
  await waitInert(tile("03 saved document"));
  await press("Escape");
  expect(dialog()).toBeNull();
  expect(tile("03 saved document").closest("[inert]")).toBeNull();
  expect(document.activeElement).toBe(tile("03 saved document"));

  await act(async () => {
    fireEvent.click(tile("01 split view"));
    await settle();
  });
  const close = within(dialog()!).getByRole("button", { name: "Close" });
  expect(document.activeElement).toBe(close);
  await press("Tab", true);
  expect(document.activeElement).toBe(within(dialog()!).getByRole("button", { name: "Next" }));
  await press("Tab");
  expect(document.activeElement).toBe(close);
  await waitInert(tile("01 split view"));
  await act(async () => {
    fireEvent.click(close);
    await settle();
  });
  expect(dialog()).toBeNull();
  expect(tile("01 split view").closest("[inert]")).toBeNull();
  expect(document.activeElement).toBe(tile("01 split view"));
});

test("pressing the dimmed page beside the lightbox leaves it open", async () => {
  await openMedia("02 custom slot");
  const backdrop = document.querySelector<HTMLElement>('[data-slot="dialog-backdrop"]');
  expect(backdrop).toBeTruthy();
  await waitInert(tile("02 custom slot"));
  await act(async () => {
    fireEvent.pointerDown(backdrop!, { button: 0 });
    fireEvent.pointerUp(backdrop!, { button: 0 });
    fireEvent.click(backdrop!);
    await settle();
  });
  expect(screen.getByRole("dialog", { name: "Screenshot 2 of 4" })).toBeTruthy();
});

test("a poll that leaves the row no media closes the lightbox onto Open run, and returning media does not reopen it", async () => {
  const item = mediaPark({ screenshots: SHOTS, videos: VIDEOS });
  await mount({ items: [item], readyRuns: [] });
  await openRow(9);
  await act(async () => {
    fireEvent.click(tile("02 custom slot"));
    await settle();
  });
  expect(dialog()).not.toBeNull();
  await waitInert(within(rows()[0]!).getByRole("link", { name: "Open run" }));
  const pr = item.pr as { screenshots: unknown[]; videos: unknown[] };
  const repoll = async () => {
    await act(async () => {
      await client!.refetchQueries({ queryKey: ["fleet"] });
      await settle();
    });
    for (let round = 0; round < 3; round += 1) await act(settle);
  };
  pr.screenshots = [];
  pr.videos = [];
  await repoll();
  expect(dialog()).toBeNull();
  expect(tileNames(rows()[0]!)).toEqual([]);
  expect(within(rows()[0]!).getByRole("link", { name: "Open run" }).closest("[inert]")).toBeNull();
  expect(document.activeElement).toBe(within(rows()[0]!).getByRole("link", { name: "Open run" }));

  pr.screenshots = SHOTS;
  pr.videos = VIDEOS;
  await repoll();
  expect(tileNames(rows()[0]!)).toEqual(TILE_NAMES);
  expect(dialog()).toBeNull();
});

test("a backticked span in a row's title shows as inline code, and its accessible name drops the backticks", async () => {
  await mount({ items: [{ ...readyPark, title: "Read a `+++` TOML header" }] });
  const button = rowButton(9);
  expect(button.getAttribute("aria-label")).toStartWith("DEMO-9 Read a +++ TOML header, ");
  expect([...button.querySelectorAll("code")].map((code) => code.textContent)).toEqual(["+++"]);
});

const writerPark = (run: number, ageMs: number, pr: object = {}): Item => ({
  ...park(run, "pull_request", ageMs, { number: run + 70, checks: "success", review: "approved", threads: 0, open_threads: 0, ...pr }),
  ticket: `WRITER-${run}`, pr_url: `https://github.com/example/writer/pull/${run + 70}`,
} as Item);

test("a run working on a pull request it is not parked on draws no Waiting row", async () => {
  const [base] = served.runs as [StatusRun];
  const fixing = { ...base, id: 20, ticket: "DEMO-20", title: "Fix the slot", phase: "working", heartbeat_age_ms: 1000, round: 2,
    pr_url: "https://github.com/example/demo/pull/2374" };
  await mount({ items: [readyPark], runs: [fixing] });
  expect(screen.getByRole("region", { name: "Running" }).textContent).toContain("DEMO-20");
  expect(keys()).toEqual(["DEMO-9"]);
  expect(section().textContent).not.toMatch(/DEMO-20|#2374/);
});

test("a row parked on a second daemon reads its ticket from that daemon and opens its run under that daemon's project", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [], writer: { items: [writerPark(7, 3 * HOUR)] }, ticketBody: WHAT });
  expect(keys()).toEqual(["WRITER-7", "DEMO-9"]);
  await openRow("WRITER-7");
  const row = rows()[0]!;
  expect(within(row).getByText("Adds a slot that takes pages.")).toBeTruthy();
  expect(seen.map((request) => request.url)).toEqual([`${WRITER}/tickets/WRITER-7`]);
  expect(within(row).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/writer/7");
  expect(within(row).getByRole("link", { name: "Open PR to review" }).getAttribute("href")).toBe("https://github.com/example/writer/pull/77");
});

test("Send back on a second daemon's row posts to that daemon", async () => {
  const seen = await mount({ items: [], actions: true, writer: { items: [writerPark(7, HOUR)] } });
  await openRow("WRITER-7");
  await act(async () => {
    fireEvent.click(within(rows()[0]!).getByRole("button", { name: "Send back with a note" }));
    await settle();
  });
  fireEvent.change(within(rows()[0]!).getByLabelText("Note for the factory"), { target: { value: "Split the change" } });
  await act(async () => {
    fireEvent.click(within(rows()[0]!).getByRole("button", { name: "Send" }));
    await settle();
  });
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${WRITER}/actions/send-back`, body: { run: 7, note: "Split the change" } }]);
});

test("the same run number on two daemons reads each daemon's own merge readiness", async () => {
  await mount({ items: [readyPark], actions: true, readyRuns: [], writer: { items: [writerPark(9, HOUR)], readyRuns: [9] } });
  const glyph = (key: string) => within(line(rows().find((row) => line(row).textContent!.startsWith(key))!)).getByRole("img");
  expect(glyph("WRITER-9").getAttribute("data-state")).toBe("ready_to_merge");
  expect(glyph("DEMO-9").getAttribute("data-state")).toBe("parked");
  expect(rowButton("WRITER-9").getAttribute("aria-label")).toBe("WRITER-9 Ticket 9, Your review, ready to merge · 1h");
  expect(rowButton("DEMO-9").getAttribute("aria-label")).toBe("DEMO-9 Ticket 9, Your review, 2h");
});

test("rows from two daemons age each on its own daemon's clock and order by those ages", async () => {
  await mount({
    items: [{ ...readyPark, asked_ms: NOW - 5 * MINUTE - 2 * MINUTE }], behindMs: 5 * MINUTE,
    writer: { items: [writerPark(7, 3 * MINUTE)], behindMs: 0 },
  });
  expect(keys()).toEqual(["WRITER-7", "DEMO-9"]);
  expect(waits().map(([, age]) => age)).toEqual(["3m", "2m"]);
});

test("a daemon that stops answering keeps its Waiting row, aged on from its last answer", async () => {
  await mount({ items: [readyPark], writer: { items: [writerPark(7, 2 * MINUTE)] } });
  expect(keys()).toEqual(["DEMO-9", "WRITER-7"]);
  expect(waits()[1]![1]).toBe("2m");
  daemons[WRITER] = { down: new Error("connection refused") };
  clock.now = NOW + 4 * MINUTE;
  await repoll();
  expect(keys()).toEqual(["DEMO-9", "WRITER-7"]);
  expect(waits()[1]![1]).toBe("6m");
});

test("a daemon that never answers leaves the other daemons' Waiting rows standing", async () => {
  await mount({ items: [readyPark, threadPark], writer: "down" });
  expect(keys()).toEqual(["DEMO-9", "DEMO-10"]);
  expect(section().querySelector("[data-count]")!.textContent).toBe("2");
});

test("a picked project's Waiting lists and counts only its daemon's rows, and a row's Open run keeps the pick", async () => {
  await mount({ items: [readyPark, threadPark], readyRuns: [], writer: { items: [writerPark(7, HOUR)] }, hash: "#/?project=writer" });
  expect(keys()).toEqual(["WRITER-7"]);
  expect(section().querySelector("[data-count]")!.textContent).toBe("1");
  await openRow("WRITER-7");
  expect(within(rows()[0]!).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/writer/7?project=writer");
});

test("a second click on an open row closes it and drops its detail, and while open its button controls that detail", async () => {
  await mount({ items: [readyPark], readyRuns: [] });
  await openRow(9);
  const detail = document.getElementById(rowButton(9).getAttribute("aria-controls")!)!;
  expect(rows()[0]!.contains(detail)).toBe(true);
  expect(within(detail).getByRole("link", { name: "Open run" })).toBeTruthy();
  await openRow(9);
  expect(rowButton(9).getAttribute("aria-expanded")).toBe("false");
  expect(rows()[0]!.hasAttribute("data-open")).toBe(false);
  expect(rows()[0]!.querySelector("[data-pr-detail]")).toBeNull();
});

test("a poll that brings an older park above an open row keeps that row open and the new row closed", async () => {
  const items = [readyPark];
  await mount({ items, readyRuns: [] });
  await openRow(9);
  items.push(park(14, "pull_request", 5 * HOUR, { number: 47, checks: "success", review: "approved", threads: 0, open_threads: 0 }));
  await repoll();
  expect(keys()).toEqual(["DEMO-14", "DEMO-9"]);
  expect(rows().map((row) => row.hasAttribute("data-open"))).toEqual([false, true]);
  expect(rowButton(14).getAttribute("aria-expanded")).toBe("false");
  expect(rowButton(9).getAttribute("aria-expanded")).toBe("true");
});

test("an open row's detail follows a readiness poll that turns it ready, gaining Merge while it stays open", async () => {
  const readyRuns: number[] = [];
  await mount({ items: [readyPark], actions: true, readyRuns });
  await openRow(9);
  expect(detailButton("Merge")).toBeNull();
  readyRuns.push(9);
  await repoll();
  expect(rowButton(9).getAttribute("aria-expanded")).toBe("true");
  expect(detailButton("Merge")!.dataset.variant).toBe("primary");
  expect(rowButton(9).getAttribute("aria-label")).toBe("DEMO-9 Ticket 9, Your review, ready to merge · 2h");
});

test("a park with no run lists as a row whose title cannot open it", async () => {
  const seen = await mount({ items: [{ ...readyPark, run: null, ticket: "DEMO-30" } as Item] });
  const button = rowButton("DEMO-30");
  expect(button.hasAttribute("disabled")).toBe(true);
  expect(button.hasAttribute("aria-expanded")).toBe(false);
  await act(async () => {
    fireEvent.click(button);
    await settle();
  });
  expect(rows()[0]!.hasAttribute("data-open")).toBe(false);
  expect(rows()[0]!.querySelector("[data-pr-detail]")).toBeNull();
  expect(seen).toEqual([]);
});

test("a park with no asked time draws no age, names no age, and sorts after the aged rows", async () => {
  await mount({ items: [{ ...threadPark, asked_ms: null } as Item, readyPark], readyRuns: [] });
  expect(keys()).toEqual(["DEMO-9", "DEMO-10"]);
  expect(rows()[1]!.querySelector("[data-age]")).toBeNull();
  expect(rowButton(10).getAttribute("aria-label")).toBe("DEMO-10 Ticket 10, Your review");
});

test("an On CI row names what it waits on in CI after its status: checks running, or green checks in their quiet period", async () => {
  const green = park(11, "ci", HOUR, { number: 44, checks: "success", review: null, threads: 0, open_threads: 0 });
  await mount({ items: [onCi, green] });
  expect(rowButton(8).getAttribute("aria-label")).toBe("DEMO-8 Ticket 8, On CI, checks running · 3h");
  expect(rowButton(11).getAttribute("aria-label")).toBe("DEMO-11 Ticket 11, On CI, checks green, quiet period · 1h");
});

test("an opened On CI row offers no Merge of either kind, even where its run's readiness would allow a bypass", async () => {
  await mount({ items: [onCi], actions: true, answers: { 8: notReady(8, "review_bypassable") } });
  await openRow(8);
  expect(waits()).toEqual([["On CI", "3h"]]);
  expect(detailButton("Merge")).toBeNull();
  expect(detailButton("Merge without review")).toBeNull();
  expect(within(rows()[0]!).getByRole("link", { name: "Open PR to review" }).dataset.variant).toBe("primary");
});
