import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Fetch } from "../../src/lib/poll";
import type { Flow, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const OLDER = "http://writer-host:7711";
const DAY = 24 * 3600 * 1000;
const NO_WAITS = { pending_checks: 0, checks_failing: 0, thread: 0, rounds_used: 0, behind_main: 0, main_red: 0, approval: 0, other: 0 };

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

/** A daemon answering each `/flow` ask with one bucket per day or week of the asked span, merges rising bucket by bucket. */
function flowDaemon(asked: URLSearchParams[], older: Flow | null): Fetch {
  const daemon = (project: string) => ({ status: { ...working, host: "writer-host", project }, attention: { level: "none", now: 0, project, items: [] } });
  const polls = peersFetch(ORIGIN, { [ORIGIN]: daemon("/srv/dev/demo"), ...(older ? { [OLDER]: daemon("/srv/dev/older") } : {}) });
  return async (url, init) => {
    if (older && url.startsWith(`${OLDER}/flow?`)) return Response.json(older);
    if (!url.startsWith(`${ORIGIN}/flow?`)) return polls(url, init);
    const query = new URL(url).searchParams;
    asked.push(query);
    const size = query.get("bucket") === "week" ? 7 * DAY : DAY;
    const now = Number(query.get("until") ?? Date.now());
    const span = Number(query.get("days")) * DAY;
    const count = Math.ceil(span / size);
    const flow: Flow = {
      now,
      buckets: Array.from({ length: count }, (_, index) => ({
        start_ms: Math.max(now - span, now - (count - index) * size),
        merged: index + 1,
        rounds_median: 1,
        interventions: 1,
        hours: { working: 2, waiting: 1, operator: 0 },
      })),
      phases: { working: 1 },
      waits: NO_WAITS,
      reverted: { count: 0, merged: 1 },
    };
    return Response.json(flow);
  };
}

async function flush() {
  for (let round = 0; round < 6; round += 1) await act(settle);
}

async function mount(hash: string, older: Flow | null = null) {
  const asked: URLSearchParams[] = [];
  happyDOM.setURL(`${ORIGIN}/`);
  window.location.hash = hash;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={ORIGIN} pollDeps={fakeDeps(flowDaemon(asked, older)).deps} />
    </QueryClientProvider>,
  );
  await flush();
  return asked;
}

const windowOf = (query: URLSearchParams) => [query.get("days"), query.get("bucket"), query.get("until")];
const openRange = () => fireEvent.click(screen.getByRole("button", { name: "Range" }));
const choices = () => within(screen.getByRole("group", { name: "Range choices" }));
const changes = () => [...document.querySelectorAll("[data-change]")].map((change) => change.textContent!);

function day(offset: number): string {
  const at = new Date();
  at.setDate(at.getDate() + offset);
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}`;
}

afterEach(() => {
  cleanup();
  window.location.hash = "";
  happyDOM.setURL("about:blank");
});

test("with no range, or a retired weekly one, in the address the menu offers 7, 30 and 90 days with 30 checked and asks for 30 daily buckets", async () => {
  for (const hash of ["#/flow?range=6w", "#/flow"]) {
    const asked = await mount(hash);
    expect(asked.length).toBeGreaterThan(0);
    expect(asked.map(windowOf)).toEqual(asked.map(() => ["30", "day", null]));
    openRange();
    expect(choices().getAllByRole("button").map((choice) => choice.textContent)).toEqual(["Last 7 days", "Last 30 days", "Last 90 days"]);
    expect(choices().getAllByRole("button", { pressed: true }).map((choice) => choice.textContent)).toEqual(["Last 30 days"]);
    cleanup();
  }
});

test("picking Last 90 days asks for 90 days in weekly buckets and checks it in the address", async () => {
  const asked = await mount("#/flow");
  openRange();
  fireEvent.click(choices().getByRole("button", { name: "Last 90 days" }));
  await flush();
  expect(windowOf(asked.at(-1)!)).toEqual(["90", "week", null]);
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("range")).toBe("90d");
});

test("each trend's change reads against the day before over Last 7 days and against the week before over Last 90 days", async () => {
  await mount("#/flow?range=7d");
  expect(changes().length).toBeGreaterThan(0);
  expect(changes().every((text) => text.endsWith(" vs the day before"))).toBe(true);
  cleanup();
  await mount("#/flow?range=90d");
  expect(changes().length).toBeGreaterThan(0);
  expect(changes().every((text) => text.endsWith(" vs the week before"))).toBe(true);
});

test("a custom range asks for its whole days ending with the To day, by day up to a month and by week beyond", async () => {
  const today = new Date();
  const until = String(new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime());
  for (const [from, bucket] of [[day(-10), "day"], [day(-40), "week"]] as const) {
    const asked = await mount("#/flow");
    openRange();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: from } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: day(-1) } });
    await flush();
    const search = new URLSearchParams(window.location.hash.split("?")[1]);
    expect([search.get("from"), search.get("to")]).toEqual([from, day(-1)]);
    expect(windowOf(asked.at(-1)!)).toEqual([bucket === "day" ? "10" : "40", bucket, until]);
    cleanup();
  }
});

test("a custom range over 182 days, or with a To day not yet ended, stays out of the address and the asks", async () => {
  for (const [from, to] of [[day(-184), day(-1)], [day(-3), day(1)]]) {
    const asked = await mount("#/flow");
    openRange();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: from } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: to } });
    await flush();
    expect(new URLSearchParams(window.location.hash.split("?")[1]).get("from")).toBeNull();
    expect(asked.every((query) => query.get("days") === "30" && !query.has("until"))).toBe(true);
    cleanup();
  }
});

test("a daemon that answers weekly rows under weeks instead of buckets is left out of the numbers", async () => {
  const week = { start_ms: Date.now() - 7 * DAY, merged: 50, rounds_median: 1, interventions: 0, hours: { working: 1, waiting: 0, operator: 0 } };
  await mount("#/flow?range=7d", { now: Date.now(), weeks: [week], phases: {}, waits: NO_WAITS, reverted: { count: 0, merged: 50 } });
  const figure = (label: string) => document.querySelector(`[data-number="${label}"]`)!.textContent;
  expect([figure("Merged"), figure("Reverted")]).toEqual([String(1 + 2 + 3 + 4 + 5 + 6 + 7), "0 of 1"]);
});
