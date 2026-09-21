import { fakeFetch } from "./actionFakes";
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { AttentionRow } from "../src/components/AttentionRow";
import { NeedsYou } from "../src/components/NeedsYou";
import { ACTIONS_OFF, NOT_WIRED } from "../src/lib/actions";
import { describe } from "../src/lib/attention";
import type { Fetch } from "../src/lib/poll";
import { storeToken } from "../src/lib/token";
import type { Attention, AttentionItem, Status } from "../src/lib/types";
import { fixture, hostOf, settle } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");
const BASE = "http://writer:7710";
const TOKEN = "tok-349";
const thresholds = allKinds.status.thresholds;

beforeEach(() => {
  localStorage.clear();
  storeToken("writer:7710", TOKEN);
});
afterEach(cleanup);

const supervisorItem = allKinds.attention.items.find((item) => item.kind === "supervisor")!;
const failedItem = allKinds.attention.items.find((item) => item.kind === "failed")!;

function renderRow(item: AttentionItem, fetchImpl: Fetch, actions = true) {
  render(
    <ul>
      <AttentionRow
        kind={item.kind}
        project="writer"
        description={describe(item, thresholds, { now: allKinds.status.now })}
        daemon={{ base: BASE, actions, fetch: fetchImpl }}
      />
    </ul>,
  );
}

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

test("Restart supervisor posts to /actions/restart-supervisor with the bearer, spins, then shows the detail", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const { seen, fetchImpl } = fakeFetch(
    { action: "restart-supervisor", ok: true, detail: "systemctl --user restart holophyte-supervise@writer-a exited 0" },
    gate,
  );
  renderRow(supervisorItem, fetchImpl);
  const restart = button("Restart supervisor");
  expect(restart.disabled).toBe(false);
  expect(restart.getAttribute("title")).toBeNull();

  fireEvent.click(restart);
  expect(seen).toEqual([
    { url: `${BASE}/actions/restart-supervisor`, method: "POST", authorization: `Bearer ${TOKEN}`, body: {} },
  ]);
  expect(restart.disabled).toBe(true);
  expect(restart.getAttribute("aria-busy")).toBe("true");
  expect(restart.querySelector("[data-spinner]")).toBeTruthy();
  expect(document.querySelector("[data-action-detail]")).toBeNull();

  await act(async () => {
    release();
    await settle();
  });
  const detail = document.querySelector("[data-action-detail]")!;
  expect(detail.textContent).toBe("systemctl --user restart holophyte-supervise@writer-a exited 0");
  expect(detail.getAttribute("data-ok")).toBe("true");
  expect(restart.disabled).toBe(false);
  expect(restart.querySelector("[data-spinner]")).toBeNull();
});

test("Requeue on a failed row posts the row's ticket to /actions/requeue; Mark needs_spec stays disabled as not wired", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "requeue", ok: true, ticket: "KO-229", detail: "KO-229 requeued" });
  renderRow(failedItem, fetchImpl);
  const mark = button("Mark needs_spec");
  expect(mark.disabled).toBe(true);
  expect(mark.getAttribute("title")).toBe(NOT_WIRED);

  await act(async () => {
    fireEvent.click(button("Requeue"));
    await settle();
  });
  expect(seen).toEqual([
    { url: `${BASE}/actions/requeue`, method: "POST", authorization: `Bearer ${TOKEN}`, body: { ticket: "KO-229" } },
  ]);
  expect(document.querySelector("[data-action-detail]")!.textContent).toBe("KO-229 requeued");
});

test("a daemon whose /status says actions: false draws every button disabled, titled with the missing opt-in", () => {
  const { seen, fetchImpl } = fakeFetch({ ok: true, detail: "never" });
  renderRow(failedItem, fetchImpl, false);
  const buttons = screen.getAllByRole("button") as HTMLButtonElement[];
  expect(buttons.map((b) => b.textContent)).toEqual(["Requeue", "Mark needs_spec"]);
  const requeue = button("Requeue");
  expect(requeue.disabled).toBe(true);
  expect(requeue.getAttribute("title")).toBe(ACTIONS_OFF);
  expect(requeue.getAttribute("title")).toContain("[serve] actions = true");
  fireEvent.click(requeue);
  expect(seen).toEqual([]);
});

test("an ok: false reply shows its detail and the button is enabled again", async () => {
  const { fetchImpl } = fakeFetch({ action: "requeue", ok: false, ticket: "KO-229", detail: "KO-229: no such ticket in the store" });
  renderRow(failedItem, fetchImpl);
  const requeue = button("Requeue");
  await act(async () => {
    fireEvent.click(requeue);
    await settle();
  });
  const detail = document.querySelector("[data-action-detail]")!;
  expect(detail.textContent).toBe("KO-229: no such ticket in the store");
  expect(detail.getAttribute("data-ok")).toBe("false");
  expect(requeue.disabled).toBe(false);
  expect(requeue.getAttribute("aria-busy")).toBeNull();
});

test("a non-2xx answer reads as ok: false naming the status and the daemon's error", async () => {
  const { fetchImpl } = fakeFetch(() => Response.json({ error: "not found", path: "/actions/requeue" }, { status: 404 }));
  renderRow(failedItem, fetchImpl);
  await act(async () => {
    fireEvent.click(button("Requeue"));
    await settle();
  });
  const detail = document.querySelector("[data-action-detail]")!;
  expect(detail.textContent).toBe(`${BASE}/actions/requeue answered 404: not found`);
  expect(detail.getAttribute("data-ok")).toBe("false");
  expect(button("Requeue").disabled).toBe(false);
});

test("the band hands each row its own daemon: the fixture's status without actions disables, actions: true enables the wired ones", async () => {
  const { seen, fetchImpl } = fakeFetch({ ok: true, detail: "done" });
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention, BASE)]} project="all" now={allKinds.status.now} actionFetch={fetchImpl} />);
  for (const row of screen.getAllByRole("listitem")) {
    for (const b of Array.from(row.querySelectorAll("button")) as HTMLButtonElement[]) expect(b.disabled).toBe(true);
  }
  cleanup();

  const status = { ...allKinds.status, actions: true };
  render(<NeedsYou hosts={[hostOf(status, allKinds.attention, BASE)]} project="all" now={allKinds.status.now} actionFetch={fetchImpl} />);
  const states = screen.getAllByRole("listitem").flatMap((row) =>
    (Array.from(row.querySelectorAll("button")) as HTMLButtonElement[]).map((b) => [b.textContent, b.disabled]),
  );
  expect(states).toEqual([
    ["Requeue", false],
    ["Mark needs_spec", true],
    ["Restart supervisor", false],
    ["Kill run", true],
    ["Requeue", false],
    ["Answer", true],
    ["Requeue", false],
  ]);
  await act(async () => {
    fireEvent.click(button("Restart supervisor"));
    await settle();
  });
  expect(seen.map((request) => request.url)).toEqual([`${BASE}/actions/restart-supervisor`]);
});

test("a pr_open row reads PR, shows the reason with the PR link, and its one action opens the URL in a new tab", () => {
  const url = "https://github.com/o/r/pull/2170";
  const item: AttentionItem = {
    kind: "pr_open",
    level: "attention",
    ticket: "REL-120",
    run: 60,
    pr_url: url,
    reason: "review requested from a coworker\n1. src/x.py:3 by @coworker",
    asked_ms: allKinds.status.now - 600000,
  };
  const opened: unknown[][] = [];
  const realOpen = window.open;
  window.open = ((...args: unknown[]) => {
    opened.push(args);
    return null;
  }) as typeof window.open;
  try {
    render(
      <ul>
        <AttentionRow
          kind={item.kind}
          project="writer"
          description={describe(item, thresholds, { now: allKinds.status.now })}
          prUrl={item.pr_url}
          daemon={{ base: BASE, actions: false, fetch: fakeFetch({}).fetchImpl }}
        />
      </ul>,
    );
    const [row] = screen.getAllByRole("listitem");
    expect(row!.querySelector("[data-kind]")!.textContent).toBe("PR");
    expect(within(row!).getByText(/^review requested from a coworker/).textContent).not.toContain("PR open:");
    const link = within(row!).getByText("PR #2170") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(url);
    expect(within(row!).getByText(/^run #60 · parked at \d\d:\d\d$/)).toBeTruthy();
    const buttons = within(row!).getAllByRole("button") as HTMLButtonElement[];
    expect(buttons.map((b) => b.textContent)).toEqual(["Open PR"]);
    expect(buttons[0]!.disabled).toBe(false);
    fireEvent.click(buttons[0]!);
    expect(opened).toEqual([[url, "_blank", "noopener,noreferrer"]]);
  } finally {
    window.open = realOpen;
  }
});

test("a pr_open row carrying pr leads with the PR link, keeps the reason's first line only, and draws three fact chips", () => {
  const url = "https://github.com/o/r/pull/2170";
  const item: AttentionItem = {
    kind: "pr_open",
    level: "attention",
    ticket: "REL-120",
    run: 60,
    pr_url: url,
    reason: "ready to merge; waiting for a human to say merge\n1. src/x.py:3 by @coworker",
    asked_ms: allKinds.status.now - 600000,
    pr: { number: 2170, checks: "success", review: "changes_requested", threads: 2 },
  };
  render(
    <ul>
      <AttentionRow
        kind={item.kind}
        project="writer"
        description={describe(item, thresholds, { now: allKinds.status.now })}
        prUrl={item.pr_url}
        daemon={{ base: BASE, actions: false, fetch: fakeFetch({}).fetchImpl }}
      />
    </ul>,
  );
  const [row] = screen.getAllByRole("listitem");
  const body = within(row!).getByText(/ready to merge/).closest("p")!;
  expect(body.textContent).toMatch(/^PR #2170\s*ready to merge; waiting for a human to say merge$/);
  expect(body.firstElementChild!.querySelector("a[data-pr]")!.getAttribute("href")).toBe(url);
  expect(row!.textContent).not.toContain("src/x.py:3");
  const chips = Array.from(row!.querySelectorAll("[data-fact]"));
  expect(chips.map((chip) => [chip.textContent, chip.getAttribute("data-tone")])).toEqual([
    ["checks green", "ok"],
    ["changes requested", "bad"],
    ["2 threads open", "warn"],
  ]);
  expect(chips[0]!.className).toContain("bg-ok-bg");
  expect(chips[1]!.className).toContain("bg-bad-bg");
  expect(chips[2]!.className).toContain("bg-warn-bg");
  expect(within(row!).getByText(/^run #60 · parked at \d\d:\d\d$/)).toBeTruthy();
});

test("a pr_open row without pr draws no fact chips and keeps the PR link after the prose", () => {
  const item: AttentionItem = {
    kind: "pr_open",
    level: "attention",
    ticket: "REL-120",
    run: 60,
    pr_url: "https://github.com/o/r/pull/2170",
    reason: "review requested from a coworker\n1. src/x.py:3 by @coworker",
  };
  render(
    <ul>
      <AttentionRow kind={item.kind} project="writer" description={describe(item, thresholds, { now: allKinds.status.now })} prUrl={item.pr_url} />
    </ul>,
  );
  const [row] = screen.getAllByRole("listitem");
  expect(row!.querySelectorAll("[data-fact]")).toHaveLength(0);
  expect(row!.textContent).toContain("src/x.py:3");
  const body = within(row!).getByText(/^review requested from a coworker/).closest("p")!;
  expect(body.textContent).toMatch(/^review requested from a coworker[\s\S]*PR #2170$/);
  expect(body.lastElementChild!.querySelector("a[data-pr]")).toBeTruthy();
});


test("a failed Needs You row opens its named run card with the frozen time box and timeline", async () => {
  const now = allKinds.status.now;
  const item = { kind: "failed", level: "attention", run: 436, ticket: "KO-436", reason: "Verification failed", ended_ms: now };
  const asked: string[] = [];
  const fetchImpl: Fetch = async url => {
    asked.push(url);
    if (url.endsWith("/runs/436")) return Response.json({
      run: { id: 436, ticket: "KO-436", title: "Failed run", phase: "done", attempt: 1,
        started_ms: now - 600000, ended_ms: now, working_ms: 600000, outcome: "failed", time_box_ms: 1800000,
        branch: "task/ko-436", host: "writer", heartbeat_age_ms: 0 },
      rounds: [{ round: 1, started_ms: now - 300000, ended_ms: now, verdict: "changes_requested", findings: [] }], events: [],
    });
    return new Response("not found", { status: 404 });
  };
  render(<NeedsYou hosts={[hostOf(allKinds.status, { ...allKinds.attention, items: [item] }, BASE)]} project="all" now={now} actionFetch={fetchImpl} />);
  fireEvent.click(screen.getByText("Verification failed"));
  await act(settle);
  expect(asked).toContain(`${BASE}/runs/436`);
  const card = screen.getByRole("article", { name: "run 436" });
  expect(within(card).getByText(/Round 1 of/)).toBeTruthy();
  expect(card.querySelector("[data-timeline]")).not.toBeNull();
  expect(card.querySelector("[data-box]")!.textContent).toBe("20m left in working box · wall 10m");
  fireEvent.keyDown(document.querySelector('[aria-expanded="true"]')!, { key: "Enter" });
  expect(screen.queryByRole("article", { name: "run 436" })).toBeNull();
});

test("a failed ticket's attempts affordance opens only its attempts card", async () => {
  const now = allKinds.status.now;
  const items = [435, 436].map((run, index) => ({
    kind: "failed", level: "attention", run, ticket: "KO-436",
    reason: `Verification failed on attempt ${index + 1}`, ended_ms: now - (1 - index) * 60000,
  }));
  const { seen, fetchImpl } = fakeFetch(() => new Response("not found", { status: 404 }));
  render(<NeedsYou hosts={[hostOf(allKinds.status, { ...allKinds.attention, items }, BASE)]} project="all" now={now} actionFetch={fetchImpl} />);
  expect(screen.queryByText("run ▾")).toBeNull();
  fireEvent.click(screen.getByText("attempts ▾"));
  await act(settle);
  const card = document.querySelector("[data-attempts-card]")!;
  expect(card).not.toBeNull();
  expect(within(card as HTMLElement).getAllByRole("listitem").map(row => row.textContent)).toEqual([
    "run #435 · Verification failed on attempt 1",
    "run #436 · Verification failed on attempt 2",
  ]);
  expect(seen).toEqual([]);
  expect(screen.queryByRole("article")).toBeNull();
  fireEvent.click(screen.getByText("hide attempts ▴"));
  expect(document.querySelector("[data-attempts-card]")).toBeNull();
});


test("parked run sends a private maintainer note to its daemon", async () => {
  const item: AttentionItem = { kind: "pr_open", level: "attention", run: 47, ticket: "KO-7", pr_url: "https://github.com/o/r/pull/7" };
  const { seen, fetchImpl } = fakeFetch({ ok: true, detail: "Sent back" });
  render(<ul><AttentionRow kind="pr_open" project="repo" runId={47}
    description={describe(item, thresholds, { now: allKinds.status.now })}
    daemon={{ base: BASE, actions: true, fetch: fetchImpl }} /></ul>);
  await act(async () => { fireEvent.click(button("Send back with note")); });
  fireEvent.change(screen.getByRole("textbox", { name: "Maintainer's note" }), { target: { value: "remove the subheader" } });
  await act(async () => { fireEvent.click(button("Send")); await settle(); });
  expect(seen).toEqual([{ url: `${BASE}/actions/send-back`, method: "POST",
    authorization: `Bearer ${TOKEN}`, body: { run: 47, note: "remove the subheader" } }]);
  expect(screen.getByRole("status").textContent).toBe("Sent back");
});
