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

interface Seen {
  url: string;
  method: string | undefined;
  authorization: string | null;
  body: unknown;
}

/** A `fetch` that records each request and answers every one with `reply`
 *  (a body, or a Response for a non-2xx answer); `gate` holds the answer
 *  until the test releases it. */
function fakeFetch(reply: Record<string, unknown> | (() => Response), gate?: Promise<void>) {
  const seen: Seen[] = [];
  const fetchImpl: Fetch = async (url, init) => {
    seen.push({
      url,
      method: init?.method,
      authorization: new Headers(init?.headers).get("authorization"),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : init?.body,
    });
    if (gate) await gate;
    return typeof reply === "function" ? reply() : Response.json(reply);
  };
  return { seen, fetchImpl };
}

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
    for (const b of within(row).getAllByRole("button") as HTMLButtonElement[]) expect(b.disabled).toBe(true);
  }
  cleanup();

  const status = { ...allKinds.status, actions: true };
  render(<NeedsYou hosts={[hostOf(status, allKinds.attention, BASE)]} project="all" now={allKinds.status.now} actionFetch={fetchImpl} />);
  const states = screen.getAllByRole("listitem").flatMap((row) =>
    (within(row).getAllByRole("button") as HTMLButtonElement[]).map((b) => [b.textContent, b.disabled]),
  );
  expect(states).toEqual([
    ["Answer", true],
    ["Requeue", false],
    ["Kill run", true],
    ["Requeue", false],
    ["Requeue", false],
    ["Mark needs_spec", true],
    ["Restart supervisor", false],
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
