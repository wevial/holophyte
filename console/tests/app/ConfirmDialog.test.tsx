import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunDetail, Status } from "../../src/lib/contract.gen";
import { ConfirmDialog } from "../../src/app/components/ConfirmDialog";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;

const classes = (element: Element) => [...element.classList];

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a confirm's box is a rounded raised surface ringed in hair-2, its label sentence case without a rule, its heading at normal weight and width", () => {
  render(
    <ConfirmDialog label="Save · holophyte" heading="Save these changes?" confirm="Save" busyLabel="Saving…" busy={false} onCancel={() => {}} onConfirm={() => {}}>
      One change.
    </ConfirmDialog>,
  );
  const box = screen.getByRole("dialog", { name: "Save these changes?" });
  expect(classes(box)).toEqual(expect.arrayContaining(["rounded-[12px]", "bg-raised"]));
  expect(classes(box).some((name) => name.startsWith("shadow-[0_0_0_1px_var(--hair-2)"))).toBe(true);
  expect(classes(box)).not.toEqual(expect.arrayContaining(["border-rule"]));
  expect(classes(box)).not.toEqual(expect.arrayContaining(["bg-paper"]));
  const label = within(box).getByText("Save · holophyte");
  expect(classes(label)).not.toContain("uppercase");
  expect(classes(label).filter((name) => name.startsWith("after:"))).toEqual([]);
  const heading = within(box).getByRole("heading", { name: "Save these changes?" });
  expect(classes(heading)).not.toContain("font-extrabold");
  expect(classes(heading).filter((name) => name.includes("font-stretch"))).toEqual([]);
});

test("the Abort confirm's reason is a field-styled textarea under a label that is not uppercase", async () => {
  window.location.hash = "#/runs/demo/12";
  const polls = fleet({ items: [], runs: [{ ...run(12, "DEMO-12"), phase: "reviewing", round: 1, max_rounds: 3 }] }, { items: [] });
  const fake = fakeDeps(async (url) => {
    if (url === `${DEMO}/runs/12`) return Response.json({ ...servedRun, run: { ...servedRun.run, id: 12, ticket: "DEMO-12", phase: "reviewing", pr_url: null } });
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true });
    return polls(url);
  });
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  const actions = document.querySelector<HTMLElement>("[data-run-actions]")!;
  await act(async () => void fireEvent.click(within(actions).getByRole("button", { name: "Abort" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
  const reason = within(screen.getByRole("dialog", { name: "Abort run 12?" })).getByRole<HTMLTextAreaElement>("textbox", { name: "Reason · saved with the run" });
  expect(reason.tagName).toBe("TEXTAREA");
  expect(classes(reason)).toContain("field");
  expect(classes(reason.labels![0]!)).not.toContain("uppercase");
});
