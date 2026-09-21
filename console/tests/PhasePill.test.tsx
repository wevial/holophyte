import sharedDetail from "../../tests/fixtures/serve/run-detail.json";
import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { RunRow } from "../src/components/RunRow";
import { PhasePill } from "../src/components/PhasePill";

afterEach(cleanup);

test("a merge gate waiting on its PR uses the neutral pill", () => {
  render(<PhasePill phase="merge_gate" pr_url="https://github.com/example/repo/pull/453" />);
  const pill = screen.getByText("monitoring PR");
  expect(pill.getAttribute("data-phase")).toBe("neutral");
  expect(pill.classList.contains("bg-well")).toBe(true);
  expect(pill.classList.contains("text-muted")).toBe(true);
  expect(pill.className).not.toContain("phase-verifying");
});

test("a collapsed Floor row reads the PR URL from run detail and refreshes it", async () => {
  let pr_url: string | null = null;
  const urls: string[] = [];
  const deps = { fetch: async (url: string | URL | Request) => {
    urls.push(String(url));
    return Response.json({ ...sharedDetail, run: { ...sharedDetail.run, pr_url } });
  } };
  const run = { id: 453, ticket: "KO-453", phase: "merge_gate", heartbeat_age_ms: 0,
    elapsed_ms: 0, time_box_ms: 100, host: "writer" };
  const row = (polls: number) => <RunRow run={run} base="http://writer:7710" polls={polls}
    deps={deps} thresholds={{ heartbeat_stale_ms: 100, strikes: 3 }} expanded={false} onToggle={() => {}} />;
  const view = render(row(1));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.getByText("verifying")).toBeTruthy();
  pr_url = "https://github.com/example/repo/pull/453";
  view.rerender(row(2));
  expect(await screen.findByText("monitoring PR")).toBeTruthy();
  expect(urls).toEqual(["http://writer:7710/runs/453", "http://writer:7710/runs/453"]);
});
