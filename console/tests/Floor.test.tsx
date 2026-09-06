import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Floor } from "../src/components/Floor";
import { Now } from "../src/components/Now";
import type { Attention, Run, Status } from "../src/lib/types";
import { NO_ATTENTION, fixture } from "./harness";

const working = await fixture<Status>("working.json");
const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

/** `working.json`'s `/status` with the run fields the daemon ticket adds. */
const RUN_52: Run = {
  id: 52,
  ticket: "KO-219",
  title: "Console: the Floor under the needs-you band",
  phase: "working",
  round: 1,
  strikes: 0,
  started_ms: working.now - 72_000,
  heartbeat_age_ms: 71_000,
  elapsed_ms: 72_000,
  time_box_ms: 1_500_000,
  host: "writer",
};
const extended: Status = { ...working, runs: [RUN_52] };

afterEach(cleanup);

const rows = () => screen.getAllByRole("listitem");
const noop = () => {};

test("working.json extended with the daemon fields renders one live block and run #52's seven cells", () => {
  render(<Floor statuses={[extended]} project="all" expandedRun={null} onToggleRun={noop} />);
  expect(screen.getByText("1 run · 1 project")).toBeTruthy();
  const block = screen.getByRole("region", { name: "writer" });
  expect(block.getAttribute("data-supervisor")).toBe("live");
  const header = block.querySelector("[data-supervisor-line]")!;
  expect(header.textContent).toBe("on writer · supervisor live · hb 12s");
  expect(header.className).toContain("text-ok-text");
  expect(rows().length).toBe(1);
  const [row] = rows();
  expect(within(row!).getByText("#52")).toBeTruthy();
  expect(within(row!).getByText("KO-219")).toBeTruthy();
  expect(within(row!).getByText(RUN_52.title!)).toBeTruthy();
  const phase = within(row!).getByText("implementing");
  expect(phase.getAttribute("data-phase")).toBe("implementing");
  expect(within(row!).getByText("1m 12s / 25m")).toBeTruthy();
  expect(within(row!).getByRole("progressbar").getAttribute("data-tone")).toBe("teal");
  const heartbeat = within(row!).getByText("hb 1m 11s");
  expect(heartbeat.getAttribute("data-heartbeat")).toBe("live");
  expect(heartbeat.className).toContain("text-ok-text");
  expect(row!.querySelector("[data-strike]")).toBeNull();
});

test("strike 2/3 is red, 1/3 amber, 0 absent", () => {
  const at = (strikes: number) => {
    cleanup();
    render(<Floor statuses={[{ ...extended, runs: [{ ...RUN_52, strikes }] }]} project="all" expandedRun={null} onToggleRun={noop} />);
    return rows()[0]!.querySelector("[data-strike]");
  };
  const two = at(2);
  expect(two?.textContent).toBe("strike 2/3");
  expect(two?.getAttribute("data-strike")).toBe("red");
  const one = at(1);
  expect(one?.textContent).toBe("strike 1/3");
  expect(one?.getAttribute("data-strike")).toBe("amber");
  expect(at(0)).toBeNull();
});

test("a heartbeat past the threshold is red bold and the stale supervisor names itself in red", () => {
  render(<Floor statuses={[allKinds.status]} project="all" expandedRun={null} onToggleRun={noop} />);
  const heartbeat = within(rows()[0]!).getByText("hb 7m 1s");
  expect(heartbeat.getAttribute("data-heartbeat")).toBe("stale");
  expect(heartbeat.className).toContain("text-bad");
  expect(heartbeat.className).toContain("font-semibold");
  const block = screen.getByRole("region", { name: "writer" });
  expect(block.getAttribute("data-supervisor")).toBe("stale");
  const header = block.querySelector("[data-supervisor-line]")!;
  expect(header.textContent).toContain("supervisor stale");
  expect(header.className).toContain("text-bad");
  expect(header.className).toContain("font-semibold");
});

test("clicking the first row then the second leaves only the second expanded", () => {
  const two: Status = { ...extended, runs: [RUN_52, { ...RUN_52, id: 53, ticket: "KO-220" }] };
  render(<Now attention={NO_ATTENTION} status={two} project="all" />);
  const toggles = () => rows().map((row) => within(row).getByRole("button").getAttribute("aria-expanded"));
  expect(toggles()).toEqual(["false", "false"]);
  fireEvent.click(within(rows()[0]!).getByRole("button"));
  expect(toggles()).toEqual(["true", "false"]);
  expect(rows()[0]!.querySelector("[data-detail]")).toBeTruthy();
  fireEvent.click(within(rows()[1]!).getByRole("button"));
  expect(toggles()).toEqual(["false", "true"]);
  expect(document.querySelectorAll("[data-detail]").length).toBe(1);
  fireEvent.click(within(rows()[1]!).getByRole("button"));
  expect(toggles()).toEqual(["false", "false"]);
});

test("an empty floor says so, and another project's selection empties it", () => {
  render(<Floor statuses={[{ ...working, runs: [] }]} project="all" expandedRun={null} onToggleRun={noop} />);
  expect(screen.getByText("Nothing on the floor")).toBeTruthy();
  expect(screen.getByText("0 runs · 1 project")).toBeTruthy();
  cleanup();
  render(<Floor statuses={[extended]} project="/srv/dev/other" expandedRun={null} onToggleRun={noop} />);
  expect(screen.getByText("Nothing on the floor")).toBeTruthy();
  expect(screen.getByText("0 runs · 0 projects")).toBeTruthy();
});
