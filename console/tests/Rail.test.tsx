import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Rail } from "../src/components/Rail";
import type { HostRecord } from "../src/lib/hosts";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf } from "./harness";

const working = await fixture<Status>("working.json");
const second = await fixture<Status>("idle_second_host.json");

afterEach(cleanup);

/** A daemon on `host` serving `project` at `port`, with `runs` runs. */
const daemon = (host: string, port: number, project: string, runs = 0): HostRecord =>
  hostOf(
    { ...working, host, target: project, project, runs: working.runs.slice(0, runs) },
    NO_ATTENTION,
    `http://${host}:${port}`,
  );

const mountRail = (hosts: HostRecord[], onProject = (_: string) => {}, project = "all") => {
  render(
    <Rail
      peers={{ hosts, polledAgo: 0, polls: 1, now: 0 }}
      view="now"
      onView={() => {}}
      project={project}
      onProject={onProject}
      theme="system"
      onTheme={() => {}}
    />,
  );
  return screen.getByRole("region", { name: "Hosts" });
};

const cards = (region: HTMLElement) => Array.from(region.querySelectorAll<HTMLElement>("[data-host-label]"));

test("four daemons under one host label are one card with four rows, each leading with its project and the port after it", () => {
  const region = mountRail([
    daemon("writer", 7710, "/srv/dev/holophyte", 1),
    daemon("writer", 7711, "/srv/dev/relos"),
    daemon("writer", 7712, "/srv/dev/atlas"),
    daemon("writer", 7713, "/srv/dev/notes"),
  ]);
  const [card, ...rest] = cards(region);
  expect(rest).toEqual([]);
  expect(card!.getAttribute("data-host-label")).toBe("writer");
  expect(card!.firstElementChild!.textContent).toBe("writer");
  const rows = within(card!).getAllByRole("button");
  expect(rows.map((row) => row.textContent)).toEqual([
    "holophyte:77101 run",
    "relos:77110 runs",
    "atlas:77120 runs",
    "notes:77130 runs",
  ]);
  expect(rows.map((row) => row.querySelector("[data-project]")!.textContent)).toEqual(["holophyte", "relos", "atlas", "notes"]);
  expect(rows.map((row) => row.querySelector("[data-port]")!.textContent)).toEqual([":7710", ":7711", ":7712", ":7713"]);
  // The name comes before the port in the row, not after.
  const first = rows[0]!;
  expect(first.querySelector("[data-project]")!.compareDocumentPosition(first.querySelector("[data-port]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  // Healthy rows say nothing about their heartbeat.
  expect(card!.textContent).not.toContain("hb");
  expect(card!.textContent).not.toContain("stale");
});

test("daemons under two host labels are two cards", () => {
  const region = mountRail([hostOf(working, NO_ATTENTION, "http://writer:7710"), hostOf(second, NO_ATTENTION, "http://writer-2:7710")]);
  expect(cards(region).map((card) => card.getAttribute("data-host-label"))).toEqual(["writer", "writer-2"]);
  expect(cards(region).map((card) => within(card).getAllByRole("button").length)).toEqual([1, 1]);
});

test("clicking a row selects that daemon's project", () => {
  const chosen: string[] = [];
  const region = mountRail([daemon("writer", 7710, "/srv/dev/holophyte"), daemon("writer", 7711, "/srv/dev/relos")], (project) => chosen.push(project));
  fireEvent.click(within(region).getByRole("button", { name: /^relos/ }));
  expect(chosen).toEqual(["/srv/dev/relos"]);
  cleanup();

  // The selected project's row is the pressed one.
  const again = mountRail([daemon("writer", 7710, "/srv/dev/holophyte"), daemon("writer", 7711, "/srv/dev/relos")], () => {}, "/srv/dev/relos");
  expect(within(again).getAllByRole("button").map((row) => row.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
});
