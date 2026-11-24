import { useRef, useState } from "react";
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

test("a confirm's box is a rounded raised surface ringed in hair-2, its label not uppercase and without a rule, its heading neither extra-bold nor stretched", () => {
  render(
    <ConfirmDialog label="Save · holophyte" heading="Save these changes?" confirm="Save" busyLabel="Saving…" busy={false} onCancel={() => {}} onConfirm={() => {}}>
      One change.
    </ConfirmDialog>,
  );
  const box = screen.getByRole("alertdialog", { name: "Save these changes?" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(classes(box)).toEqual(expect.arrayContaining(["rounded-[12px]", "bg-raised"]));
  expect(classes(box).some((name) => name.startsWith("shadow-[0_0_0_1px_var(--hair-2)"))).toBe(true);
  expect(classes(box)).not.toContain("border-rule");
  expect(classes(box)).not.toContain("bg-paper");
  const label = within(box).getByText("Save · holophyte");
  expect(classes(label)).not.toContain("uppercase");
  expect(classes(label).filter((name) => name.startsWith("after:"))).toEqual([]);
  const heading = within(box).getByRole("heading", { name: "Save these changes?" });
  expect(classes(heading)).not.toContain("font-extrabold");
  expect(classes(heading).filter((name) => name.includes("font-stretch"))).toEqual([]);
});

test("the Abort confirm's reason is a field-styled textarea under a label that is not uppercase", async () => {
  window.location.hash = "#/runs/demo/12";
  const polls = fleet({ items: [], runs: [run(12, "DEMO-12")] }, { items: [] });
  const fake = fakeDeps(async (url) => {
    if (url === `${DEMO}/runs/12`) return Response.json({ ...servedRun, run: { ...servedRun.run, id: 12 } });
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
  const reason = within(screen.getByRole("alertdialog", { name: "Abort run 12?" })).getByRole<HTMLTextAreaElement>("textbox", { name: "Reason · saved with the run" });
  expect(reason.tagName).toBe("TEXTAREA");
  expect(classes(reason)).toContain("field");
  expect(classes(reason.labels![0]!)).not.toContain("uppercase");
});

test("a confirm exposes role alertdialog, not dialog, and pressing the dimmed page outside it leaves it open without calling onCancel", async () => {
  let cancelled = 0;
  render(
    <div>
      <button type="button">Outside</button>
      <ConfirmDialog
        label="Save · holophyte"
        heading="Save these changes?"
        confirm="Save"
        busyLabel="Saving…"
        busy={false}
        onCancel={() => {
          cancelled += 1;
        }}
        onConfirm={() => {}}
      >
        One change.
      </ConfirmDialog>
    </div>,
  );
  await act(settle);
  expect(screen.getByRole("alertdialog", { name: "Save these changes?" })).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
  const backdrop = document.querySelector<HTMLElement>('[data-slot="alert-dialog-backdrop"]');
  expect(backdrop).toBeTruthy();
  const outside = screen.getByRole("button", { name: "Outside" });
  for (const target of [backdrop!, outside]) {
    await act(async () => {
      fireEvent.pointerDown(target, { button: 0 });
      fireEvent.pointerUp(target, { button: 0 });
      fireEvent.click(target);
    });
  }
  expect(screen.getByRole("alertdialog", { name: "Save these changes?" })).toBeTruthy();
  expect(cancelled).toBe(0);
});

async function waitInert(element: HTMLElement) {
  for (let attempt = 0; attempt < 25 && element.closest("[inert]") == null; attempt += 1) {
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 10)));
  }
  expect(element.closest("[inert]")).not.toBeNull();
}

test("closing a confirm by Escape, Cancel or confirm-settle lifts inert so the caller's opener keeps focus", async () => {
  function Caller() {
    const [open, setOpen] = useState(false);
    const opener = useRef<HTMLButtonElement>(null);
    const close = () => {
      setOpen(false);
      opener.current?.focus();
    };
    return (
      <div>
        <button type="button">Earlier focus</button>
        <button ref={opener} type="button" onClick={() => setOpen(true)}>
          Open confirm
        </button>
        {open && (
          <ConfirmDialog label="Save · holophyte" heading="Save these changes?" confirm="Save" busyLabel="Saving…" busy={false} onCancel={close} onConfirm={close}>
            One change.
          </ConfirmDialog>
        )}
      </div>
    );
  }
  render(<Caller />);
  const earlier = screen.getByRole("button", { name: "Earlier focus" });
  const opener = screen.getByRole("button", { name: "Open confirm" });

  for (const close of [
    () => fireEvent.keyDown(document.activeElement!, { key: "Escape" }),
    () => fireEvent.click(screen.getByRole("button", { name: "Cancel" })),
    () => fireEvent.click(screen.getByRole("button", { name: "Save" })),
  ]) {
    earlier.focus();
    await act(async () => void fireEvent.click(opener));
    expect(screen.getByRole("alertdialog", { name: "Save these changes?" })).toBeTruthy();
    await waitInert(opener);
    await act(async () => void close());
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(opener.closest("[inert]")).toBeNull();
    expect(document.activeElement === opener).toBe(true);
  }
});
