import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Now } from "../src/components/Now";
import { RunDetail } from "../src/components/RunDetail";
import { formatClock } from "../src/lib/format";
import type { Fetch } from "../src/lib/poll";
import type { Run, RunDetailBody, RunFilesBody, Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf, settle } from "./harness";

const MINUTE = 60_000;
const T = 1_756_900_000_000;
const BASE = "http://writer:7710";

const working = await fixture<Status>("working.json");

/** `/runs/91` as the daemon serves it: two rounds, the newest open. */
const DETAIL: RunDetailBody = {
  run: {
    id: 91,
    ticket: "KO-232",
    title: "Console: the run detail",
    phase: "reviewing",
    attempt: 1,
    started_ms: T,
    ended_ms: null,
    outcome: null,
    time_box_ms: 30 * MINUTE,
    branch: "task/ko-232",
    host: "writer",
    heartbeat_age_ms: 4_000,
    merge_sha: null,
    max_rounds: 2,
  },
  rounds: [
    {
      round: 1,
      started_ms: T + 8 * MINUTE,
      ended_ms: T + 12 * MINUTE,
      verdict: "changes_requested",
      findings: [{ path: "old.py", line: 1, severity: "must", message: "addressed since" }],
    },
    {
      round: 2,
      started_ms: T + 16 * MINUTE,
      ended_ms: null,
      verdict: "changes_requested",
      findings: [
        { path: "holophyte/serve.py", line: 12, severity: "nit", message: "Trailing comma" },
        { path: "holophyte/runs.py", line: 40, severity: "must", message: "Lease is never released" },
        { path: "holophyte/review.py", severity: "should", message: "Name the round in the log" },
      ],
    },
  ],
  events: [],
};

/** Files as `/runs/91/files` serves them: the branch has one change so far. */
const FILES: RunFilesBody = {
  files: [{ path: "holophyte/serve.py", status: "M", added: 12, deleted: 3 }],
  total_added: 12,
  total_deleted: 3,
};

const answering =
  (body: RunDetailBody, files: Response | (() => Response) = () => Response.json(FILES)): Fetch =>
  async (url) => {
    if (url.endsWith("/runs/91/files")) return typeof files === "function" ? files() : files;
    return url.endsWith("/runs/91") ? Response.json(body) : new Response("not found", { status: 404 });
  };

afterEach(cleanup);

async function mount(body: RunDetailBody, now: number, files?: () => Response) {
  render(<RunDetail base={BASE} id={91} now={now} polls={1} deps={{ fetch: answering(body, files) }} />);
  await settle();
}

test("the newest round's findings are cards pilled must, should, nit with path:line and a 1 must · 1 should label", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  const cards = screen.getAllByRole("listitem").filter((item) => item.hasAttribute("data-finding"));
  expect(cards.length).toBe(3);
  expect(cards.map((card) => card.querySelector("[data-severity]")!.textContent)).toEqual(["must", "should", "nit"]);
  expect(cards.map((card) => card.querySelector("[data-severity]")!.getAttribute("data-severity"))).toEqual([
    "must",
    "should",
    "nit",
  ]);
  expect(cards[0]!.querySelector("[data-severity]")!.className).toContain("bg-bad-bg");
  expect(cards[1]!.querySelector("[data-severity]")!.className).toContain("bg-warn-bg");
  expect(cards.map((card) => card.querySelector("[data-location]")!.textContent)).toEqual([
    "holophyte/runs.py:40",
    "holophyte/review.py",
    "holophyte/serve.py:12",
  ]);
  expect(within(cards[0]!).getByText("Lease is never released")).toBeTruthy();
  expect(document.querySelector("[data-severity-counts]")!.textContent).toBe("1 must · 1 should");
  expect(document.querySelector("[data-files-label]")!.textContent).toBe("1 · +12 −3");
  expect(document.querySelector("[data-file] [data-path]")!.textContent).toBe("holophyte/serve.py");
  expect(screen.getByText("Round 2 of 2 · reviewing")).toBeTruthy();
  expect(document.querySelector("[data-started]")!.textContent).toBe(`started ${formatClock(T)} · writer`);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("10m 00s left in box");
  expect(box.getAttribute("data-box")).toBe("left");
  const timeline = screen.getByRole("list", { name: "Round timeline" });
  const items = Array.from(timeline.children) as HTMLElement[];
  expect(items.map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "review", "fix", "review", "remaining"]);
  expect(items[3]!.getAttribute("data-running")).toBe("true");
  expect(items[3]!.querySelector(".segment-running")).toBeTruthy();
  expect(items[0]!.querySelector(".segment-running")).toBeNull();
  const actions = Array.from(document.querySelectorAll("footer button")) as HTMLButtonElement[];
  const buttons = actions.map((button) => [button.textContent, button.disabled]);
  expect(buttons).toEqual([
    ["Kill run", true],
    ["Requeue ticket", true],
  ]);
});

test("the header shows no sha for an unmerged run, the plain short sha when merged, and an anchor when commit_url is set", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  expect(document.querySelector("[data-sha]")).toBeNull();
  cleanup();

  await mount({ ...DETAIL, run: { ...DETAIL.run, merge_sha: "3f9c2ab0c1d2e3f4", commit_url: null } }, T + 20 * MINUTE);
  const plain = document.querySelector("header [data-sha]")!;
  expect(plain.tagName).toBe("SPAN");
  expect(plain.textContent).toBe("3f9c2ab");
  cleanup();

  const url = "https://github.com/example/writer/commit/3f9c2ab0c1d2e3f4";
  await mount({ ...DETAIL, run: { ...DETAIL.run, merge_sha: "3f9c2ab0c1d2e3f4", commit_url: url } }, T + 20 * MINUTE);
  const linked = document.querySelector("header [data-sha]") as HTMLAnchorElement;
  expect(linked.tagName).toBe("A");
  expect(linked.getAttribute("href")).toBe(url);
  expect(linked.getAttribute("target")).toBe("_blank");
  expect(linked.getAttribute("rel")).toBe("noopener noreferrer");
  expect(linked.textContent).toBe("3f9c2ab");
});

test("the header shows a PR #N anchor when pr_url is set and none otherwise", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  expect(document.querySelector("header [data-pr]")).toBeNull();
  cleanup();

  const url = "https://github.com/o/r/pull/2170";
  await mount({ ...DETAIL, run: { ...DETAIL.run, pr_url: url } }, T + 20 * MINUTE);
  const pr = document.querySelector("header [data-pr]") as HTMLAnchorElement;
  expect(pr.tagName).toBe("A");
  expect(pr.textContent).toBe("PR #2170");
  expect(pr.getAttribute("href")).toBe(url);
  expect(pr.getAttribute("target")).toBe("_blank");
  expect(pr.getAttribute("rel")).toBe("noopener noreferrer");
});

test("past the box the header reads 10m 00s over the box in the bad tone and the segments fill the bar", async () => {
  await mount(DETAIL, T + 40 * MINUTE);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("10m 00s over the box");
  expect(box.getAttribute("data-box")).toBe("over");
  expect(box.className).toContain("text-bad");
  const timeline = screen.getByRole("list", { name: "Round timeline" });
  const items = Array.from(timeline.children) as HTMLElement[];
  expect(items.map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "review", "fix", "review"]);
  // Each item is `calc(P% - Qpx)`: the percentages sum to 100 and the px
  // subtractions sum to the three 3px gaps, so items plus gaps fit the bar.
  const parts = items.map((item) => {
    const match = /^calc\((\S+)% - (\S+)px\)$/.exec(item.style.width);
    expect(match).toBeTruthy();
    return { percent: parseFloat(match![1]!), px: parseFloat(match![2]!) };
  });
  expect(parts.reduce((sum, part) => sum + part.percent, 0)).toBeCloseTo(100, 6);
  expect(parts.reduce((sum, part) => sum + part.px, 0)).toBeCloseTo(3 * (items.length - 1), 6);
  expect(timeline.style.gap).toBe("3px");
});

test("a newest round that passed shows no open findings and zero counts", async () => {
  const passed: RunDetailBody = {
    ...DETAIL,
    rounds: [DETAIL.rounds[0]!, { ...DETAIL.rounds[1]!, ended_ms: T + 19 * MINUTE, verdict: "pass", findings: [] }],
  };
  await mount(passed, T + 20 * MINUTE);
  expect(screen.getByText("No open findings")).toBeTruthy();
  expect(document.querySelector("[data-severity-counts]")!.textContent).toBe("0 must · 0 should");
  expect(document.querySelector("[data-finding]")).toBeNull();
});

test("a files endpoint answering 409 leaves one line, its own message, and the rest of the card renders", async () => {
  const gone = () => Response.json({ error: "branch task/ko-232 is not on disk", run: 91 }, { status: 409 });
  await mount({ ...DETAIL, events: [{ at: T, kind: "claimed", summary: "claimed KO-232" }] }, T + 20 * MINUTE, gone);
  const column = screen.getByRole("region", { name: "Files touched" });
  expect(column.querySelector("[data-files-note]")!.textContent).toBe("branch task/ko-232 is not on disk");
  expect(column.querySelector("[data-file]")).toBeNull();
  expect(column.querySelector("[data-files-label]")).toBeNull();
  expect(screen.getByText("Round 2 of 2 · reviewing")).toBeTruthy();
  expect(document.querySelectorAll("[data-finding]").length).toBe(3);
  expect(document.querySelector("[data-log-summary]")!.textContent).toBe("1 event · last: claimed KO-232 20m ago");
  expect(document.querySelectorAll("[data-log-row]").length).toBe(1);
  expect(document.querySelector("[data-detail-error]")).toBeNull();
});

test("a 404 says the run is not in the store and the Floor row still collapses and re-expands", async () => {
  const run: Run = { ...working.runs[0]!, id: 91, started_ms: T };
  const status: Status = { ...working, now: T + 20 * MINUTE, runs: [run] };
  const missing: Fetch = async () => Response.json({ error: "no such run", run: 91 }, { status: 404 });
  render(<Now hosts={[hostOf(status, NO_ATTENTION, BASE)]} project="all" now={status.now} deps={{ fetch: missing }} />);
  const row = screen.getByRole("listitem");
  const toggle = within(row).getByRole("button");
  fireEvent.click(toggle);
  await settle();
  expect(row.querySelector("[data-detail-error]")!.textContent).toBe("run 91 is not in the store");
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(row.querySelector("[data-detail]")).toBeNull();
  fireEvent.click(toggle);
  await settle();
  expect(row.querySelector("[data-detail-error]")!.textContent).toBe("run 91 is not in the store");
});
