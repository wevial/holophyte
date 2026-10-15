import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { Attention, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, needsDaemons } from "./needsFleet";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

type Item = Attention["items"][number];
const one = blocked;
const two: Item = { ...one, ticket: "KO-11", run: 5 };

function statusOf(project: string, runs: number, admission: string): Status {
  const run = working.runs[0]!;
  return {
    ...working,
    host: "writer-host",
    project,
    admission,
    runs: Array.from({ length: runs }, (_, at) => ({ ...run, id: run.id + at })),
  };
}

const attentionOf = (items: Item[], project: string): Attention => ({ level: items.length ? "attention" : "none", now: 0, project, items });

const daemons = peersFetch(ORIGIN, {
  [ORIGIN]: { status: statusOf("/srv/dev/demo", 1, "held"), attention: attentionOf([one, two], "/srv/dev/demo") },
  [PEER]: { status: statusOf("/srv/dev/writer", 2, "enabled"), attention: attentionOf([], "/srv/dev/writer") },
});

async function mount(hash: string, fetchImpl: Fetch = daemons) {
  window.location.hash = hash;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={ORIGIN} pollDeps={fakeDeps(fetchImpl).deps} />
    </QueryClientProvider>,
  );
  await act(settle);
  await act(settle);
}

const location = () => {
  const [path, query = ""] = window.location.hash.slice(1).split("?");
  return { path, project: new URLSearchParams(query).get("project") };
};
const switcher = () => screen.getByRole("button", { name: /Switch project$/ });
const find = () => within(screen.getByRole("dialog", { name: "Switch project" })).getByRole("combobox");
const activeName = () => document.getElementById(find().getAttribute("aria-activedescendant")!)?.textContent;
const badge = () => screen.getByRole("navigation", { name: "Pages" }).querySelector("[data-badge]");
const optionNames = () => screen.getAllByRole("option").map((option) => option.textContent);

async function key(target: HTMLElement, name: string) {
  fireEvent.keyDown(target, { key: name });
  await act(settle);
}

async function type(value: string) {
  fireEvent.change(find(), { target: { value } });
  await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => sessionStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("ArrowDown on the button opens the find box at the current scope, and typing then Enter scopes the route", async () => {
  await mount("#/runs");
  switcher().focus();
  await key(switcher(), "ArrowDown");
  expect(switcher().getAttribute("aria-expanded")).toBe("true");
  expect(document.activeElement).toBe(find());
  expect(find().getAttribute("aria-controls")).toBe(screen.getByRole("listbox", { name: "Projects" }).id);
  expect(activeName()).toContain("All projects");

  await type("wr");
  expect(activeName()).toContain("writer");
  await key(find(), "Enter");
  expect(location()).toEqual({ path: "/runs", project: "writer" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(switcher());
  expect(switcher().getAttribute("aria-label")).toBe("Project: writer. Switch project");
});

test("arrows wrap, Esc closes without moving, and Enter with no match does nothing", async () => {
  await mount("#/runs");
  await key(switcher(), "ArrowDown");
  expect(optionNames()).toHaveLength(3);
  await key(find(), "ArrowUp");
  expect(activeName()).toContain("writer");
  await key(find(), "ArrowDown");
  expect(activeName()).toContain("All projects");

  await key(find(), "Escape");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(location()).toEqual({ path: "/runs", project: null });
  expect(document.activeElement).toBe(switcher());

  await key(switcher(), "ArrowUp");
  await type("zz");
  expect(screen.getByText("No project matches.")).toBeTruthy();
  expect(screen.queryByRole("listbox")).toBeNull();
  await key(find(), "Enter");
  expect(screen.getByRole("dialog", { name: "Switch project" })).toBeTruthy();
  expect(location()).toEqual({ path: "/runs", project: null });
});

test("a held scope shows its tag and need count, and picking All projects drops the scope but keeps the route", async () => {
  await mount("#/tickets?project=demo");
  const button = switcher();
  expect(button.getAttribute("aria-label")).toBe("Project: demo, held, 2 need you. Switch project");
  expect(button.textContent).toBe("demoheld2");

  fireEvent.click(button);
  await act(settle);
  const options = screen.getAllByRole("option");
  expect(options.filter((option) => option.getAttribute("aria-selected") === "true").map((o) => o.textContent)).toEqual([
    "demoheld21 open",
  ]);
  expect(optionNames()).toEqual(["All projects23 open", "demoheld21 open", "writer2 open"]);
  fireEvent.click(options[0]!);
  await act(settle);
  expect(location()).toEqual({ path: "/tickets", project: null });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(switcher().getAttribute("aria-label")).toBe("Project: All projects, 2 need you. Switch project");
});

test("each row counts its project's Needs you entries, so a pull request not ready to merge counts nowhere", async () => {
  await mount("#/runs", needsDaemons());
  expect(switcher().getAttribute("aria-label")).toBe("Project: All projects, 3 need you. Switch project");
  fireEvent.click(switcher());
  await act(settle);
  expect(optionNames()).toEqual(["All projects30 open", "demo20 open", "writer10 open"]);
  cleanup();

  await mount("#/runs", needsDaemons(false));
  expect(switcher().getAttribute("aria-label")).toBe("Project: All projects, 2 need you. Switch project");
  expect(badge()!.textContent).toBe("2");
  fireEvent.click(switcher());
  await act(settle);
  expect(optionNames()).toEqual(["All projects20 open", "demo20 open", "writer0 open"]);
});

test("a bare open restores the session's scope on Overview, while a named route stays as linked", async () => {
  await mount("#/runs");
  await key(switcher(), "ArrowDown");
  await type("writer");
  await key(find(), "Enter");
  cleanup();

  await mount("");
  expect(location()).toEqual({ path: "/", project: "writer" });
  expect(switcher().getAttribute("aria-label")).toBe("Project: writer. Switch project");
  cleanup();

  await mount("#/runs");
  expect(location()).toEqual({ path: "/runs", project: null });
});

test("with session storage throwing, a bare open is unscoped and picking still works", async () => {
  sessionStorage.setItem("holophyte.next.project", "writer");
  const real = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage")!;
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    get: () => {
      throw new Error("storage disabled");
    },
  });
  try {
    await mount("");
    expect(location().project).toBeNull();
    await key(switcher(), "ArrowDown");
    await type("demo");
    await key(find(), "Enter");
    expect(location()).toEqual({ path: "/", project: "demo" });
  } finally {
    Object.defineProperty(globalThis, "sessionStorage", real);
  }
});
