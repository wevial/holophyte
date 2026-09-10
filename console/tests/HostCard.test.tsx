import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { HostCard } from "../src/components/HostCard";
import { groupByHost, type HostRecord } from "../src/lib/hosts";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf } from "./harness";

const working = await fixture<Status>("working.json");
const staleSupervisor = await fixture<Status>("stale_supervisor.json");

afterEach(cleanup);

const withDaemon = (status: Status, upMs: number): Status => ({ ...status, daemon: { started_ms: status.now - upMs, pid: 4000 } });

const mountCard = (hosts: HostRecord[]) => {
  const [group] = groupByHost(hosts);
  render(<HostCard group={group!} project="all" onProject={() => {}} />);
  return document.querySelector<HTMLElement>("[data-host-label]")!;
};

const row = (address: string) => document.querySelector<HTMLElement>(`[data-host="${address}"]`)!;

test("a fresh supervisor heartbeat is not shown; one past the threshold reads supervisor stale with its age in the bad tone", () => {
  // working.json: hb 12s against a 3m threshold; stale_supervisor.json: hb 4m past it.
  const card = mountCard([
    hostOf(working, NO_ATTENTION, "http://writer:7710"),
    hostOf({ ...staleSupervisor, target: "/srv/dev/relos" }, NO_ATTENTION, "http://writer:7711"),
  ]);

  const fresh = row("writer:7710");
  expect(fresh.textContent).toBe("writer:77101 run");
  expect(fresh.textContent).not.toContain("12s");
  expect(fresh.hasAttribute("data-stale")).toBe(false);
  expect(fresh.querySelector("[data-tail]")!.className).not.toContain("text-rail-bad-text");

  const stale = row("writer:7711");
  expect(stale.querySelector("[data-tail]")!.textContent).toBe("supervisor stale 4m");
  expect(stale.querySelector("[data-tail]")!.className).toContain("text-rail-bad-text");
  expect(stale.getAttribute("data-stale")).toBe("true");
  expect(stale.querySelector("[aria-hidden]")!.className).toContain("bg-bad");
  expect(fresh.querySelector("[aria-hidden]")!.className).toContain("bg-ok");
  // The rail is 220px wide: a fault beside the name would squeeze the
  // project out, so the fault takes its own line under the name while the
  // healthy run count shares the name's line.
  const lineOf = (el: Element) => el.closest("[data-line]");
  expect(lineOf(fresh.querySelector("[data-tail]")!)).toBe(lineOf(fresh.querySelector("[data-project]")!));
  expect(lineOf(stale.querySelector("[data-tail]")!)).not.toBe(lineOf(stale.querySelector("[data-project]")!));
  expect(stale.querySelectorAll("[data-line]").length).toBe(2);
  // The card carries the border for its stale daemon.
  expect(card.className).toContain("border-bad/50");
});

test("a daemon that did not answer reads no answer in the bad tone, keeping its project name from the last good status", () => {
  const lost: HostRecord = { ...hostOf(working, NO_ATTENTION, "http://writer:7710"), error: "http://writer:7710/status timed out" };
  mountCard([lost]);
  const entry = row("writer:7710");
  expect(entry.querySelector("[data-project]")!.textContent).toBe("writer");
  expect(entry.querySelector("[data-tail]")!.textContent).toBe("no answer");
  expect(entry.querySelector("[data-tail]")!.className).toContain("text-rail-bad-text");
  expect(entry.getAttribute("data-unreachable")).toBe("true");
  expect(entry.hasAttribute("data-stale")).toBe(false);
});

test("the foot says how long the daemons have been up: the shortest when they disagree, nothing when none said", () => {
  const HOUR = 3_600_000;
  const card = mountCard([
    hostOf(withDaemon(working, 12 * HOUR + 20_000), NO_ATTENTION, "http://writer:7710"),
    hostOf(withDaemon({ ...working, target: "/srv/dev/relos" }, 12 * HOUR), NO_ATTENTION, "http://writer:7711"),
  ]);
  expect(card.querySelector("[data-foot]")!.textContent).toBe("daemons up 12h");
  cleanup();

  const skewed = mountCard([
    hostOf(withDaemon(working, 12 * HOUR), NO_ATTENTION, "http://writer:7710"),
    hostOf(withDaemon({ ...working, target: "/srv/dev/relos" }, 3 * HOUR), NO_ATTENTION, "http://writer:7711"),
  ]);
  expect(skewed.querySelector("[data-foot]")!.textContent).toBe("daemons up 3h");
  cleanup();

  const silent = mountCard([hostOf(working, NO_ATTENTION, "http://writer:7710")]);
  expect(silent.querySelector("[data-foot]")).toBeNull();
});
