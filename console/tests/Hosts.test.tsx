import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen, within } from "@testing-library/react";
import { WRITES_LATER } from "../src/components/ActionButton";
import { Hosts } from "../src/components/Hosts";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf } from "./harness";

const working = await fixture<Status>("working.json");
const staleSupervisor = await fixture<Status>("stale_supervisor.json");
const second = await fixture<Status>("idle_second_host.json");

afterEach(cleanup);

const withDaemon = (status: Status, upMs: number): Status => ({
  ...status,
  daemon: { started_ms: status.now - upMs, pid: 4000 },
});

test("a stale supervisor reads stale · pid · hb in bold bad; a live one reads in the ok colour", () => {
  const DAY = 86_400_000;
  const hosts = [
    hostOf(withDaemon(staleSupervisor, 3 * DAY + 4 * 3_600_000), NO_ATTENTION, "http://writer:7710"),
    hostOf(withDaemon(second, 11 * 3_600_000), NO_ATTENTION, "http://writer-2:7710"),
  ];
  render(<Hosts hosts={hosts} project="all" now={0} />);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Hosts & daemons");
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("2 hosts · 2 daemons on :7710");

  const stale = screen.getByRole("article", { name: "writer" });
  const staleCell = stale.querySelector("[data-supervisor]")!;
  expect(staleCell.textContent).toBe("stale · pid 4242 · hb 4m");
  expect(staleCell.getAttribute("data-supervisor")).toBe("stale");
  expect(staleCell.className).toContain("text-bad-text");
  expect(staleCell.className).toContain("font-semibold");
  expect(stale.querySelector("[data-daemon]")!.textContent).toBe("up 3d 4h");
  expect(stale.querySelector("[data-runs]")!.textContent).toBe("0 active");

  const live = screen.getByRole("article", { name: "writer-2" });
  const liveCell = live.querySelector("[data-supervisor]")!;
  expect(liveCell.textContent).toBe("live · pid 4343 · hb 9s");
  expect(liveCell.className).toContain("text-ok-text");
  expect(liveCell.className).not.toContain("font-semibold");
  expect(live.querySelector("[data-daemon]")!.textContent).toBe("up 11h");
  expect(within(live).getByText("/srv/dev/writer-2")).toBeTruthy();

  const actions = within(live).getAllByRole("button");
  expect(actions.map((button) => button.textContent)).toEqual(["Restart supervisor", "Open daemon log"]);
  for (const button of actions) {
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.getAttribute("title")).toBe(WRITES_LATER);
  }
});

test("an unreachable daemon's card says so with the last good answer's age in place of the cells", () => {
  const lost = { ...hostOf(working, NO_ATTENTION, "http://writer:7710", 1_000), error: "connection refused", polled_ms: 41_000 };
  render(<Hosts hosts={[lost]} project="all" now={41_000} />);
  const card = screen.getByRole("article", { name: "writer" });
  expect(card.getAttribute("data-unreachable")).toBe("true");
  expect(card.querySelector("[data-unreachable-line]")!.textContent).toBe("unreachable · last seen 40s ago");
  expect(card.querySelector("[data-supervisor]")).toBeNull();
  expect(card.className).toContain("border-bad/50");
});

test("the selected project keeps only its daemon's card", () => {
  const hosts = [hostOf(working, NO_ATTENTION, "http://writer:7710"), hostOf(second, NO_ATTENTION, "http://writer-2:7710")];
  render(<Hosts hosts={hosts} project="/srv/dev/writer-2" now={0} />);
  expect(screen.getAllByRole("article").map((card) => card.getAttribute("aria-label"))).toEqual(["writer-2"]);
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("1 host · 1 daemon on :7710");
});
