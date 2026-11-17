import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Attention, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, peersFetch, settle, type StubDaemon } from "../harness";
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

const LOTUS = "http://writer-host:7712";
const threeProjects = peersFetch(ORIGIN, {
  [ORIGIN]: { status: statusOf("/srv/dev/demo", 1, "enabled"), attention: attentionOf([], "/srv/dev/demo") },
  [PEER]: { status: statusOf("/srv/dev/halo", 1, "enabled"), attention: attentionOf([], "/srv/dev/halo") },
  [LOTUS]: { status: statusOf("/srv/dev/Lotuspod", 1, "enabled"), attention: attentionOf([], "/srv/dev/Lotuspod") },
});

const accented = peersFetch(ORIGIN, {
  [ORIGIN]: { status: statusOf("/srv/dev/café", 1, "enabled"), attention: attentionOf([], "/srv/dev/café") },
  [PEER]: { status: statusOf("/srv/dev/cafe", 1, "enabled"), attention: attentionOf([], "/srv/dev/cafe") },
});

const coloured = peersFetch(ORIGIN, {
  [ORIGIN]: { status: { ...statusOf("/srv/dev/demo", 1, "enabled"), colour: "violet" }, attention: attentionOf([], "/srv/dev/demo") },
  [PEER]: { status: { ...statusOf("/srv/dev/writer", 1, "enabled"), colour: "teal" }, attention: attentionOf([], "/srv/dev/writer") },
});

async function mount(hash: string, fetchImpl: Fetch = daemons) {
  window.location.hash = hash;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <ConsoleApp base={ORIGIN} pollDeps={fakeDeps(fetchImpl).deps} />
    </QueryClientProvider>,
  );
  await act(settle);
  await act(settle);
  return queries;
}

const location = () => {
  const [path, query = ""] = window.location.hash.slice(1).split("?");
  return { path, project: new URLSearchParams(query).get("project") };
};
const switcher = () => screen.getByRole("button", { name: /Switch project$/ });
const dots = () => Array.from(switcher().querySelectorAll<HTMLElement>("[data-dot]"), (dot) => dot.style.background);
const find = () => within(screen.getByRole("dialog", { name: "Switch project" })).getByRole("combobox");
const activeName = () => document.getElementById(find().getAttribute("aria-activedescendant")!)?.textContent;
const badge = () => screen.getByRole("navigation", { name: "Pages" }).querySelector("[data-badge]");
const options = () => within(screen.getByRole("listbox", { name: "Projects" })).getAllByRole("option");
const optionNames = () => options().map((option) => option.textContent);
const optionDots = () => options().map((option) => Array.from(option.querySelectorAll<HTMLElement>("[data-dot]"), (dot) => dot.style.background));
const checked = () => options().filter((option) => option.querySelector("[data-check]")).map((option) => option.textContent);

async function key(target: HTMLElement, name: string) {
  fireEvent.keyDown(target, { key: name });
  await act(settle);
}

async function type(value: string) {
  fireEvent.change(find(), { target: { value } });
  await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${ORIGIN}/`));
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

test("a held scope with items waiting shows its tag but no count on the button, while the button's name still says how many wait", async () => {
  await mount("#/tickets?project=demo");
  const button = switcher();
  expect(button.getAttribute("aria-label")).toBe("Project: demo, held, 2 waiting on you. Switch project");
  expect(button.textContent).toBe("demoheld");
  expect(button.querySelector("[data-need]")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  try {
    expect(switcher().textContent).toBe("");
    expect(switcher().getAttribute("aria-label")).toBe("Project: demo, held, 2 waiting on you. Switch project");
  } finally {
    localStorage.clear();
  }
});

test("the current scope's option alone carries the check, and picking All projects drops the scope but keeps the route", async () => {
  await mount("#/tickets?project=demo");
  fireEvent.click(switcher());
  await act(settle);
  expect(options().filter((option) => option.getAttribute("aria-selected") === "true").map((o) => o.textContent)).toEqual(["demoheld"]);
  expect(checked()).toEqual(["demoheld"]);
  fireEvent.click(options()[0]!);
  await act(settle);
  expect(location()).toEqual({ path: "/tickets", project: null });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(switcher().getAttribute("aria-label")).toBe("Project: All projects, 2 waiting on you. Switch project");
  fireEvent.click(switcher());
  await act(settle);
  expect(checked()).toEqual(["All projects"]);
});

test("the button's name counts the scope's Waiting on you entries whether or not a pull request is ready", async () => {
  for (const writerReady of [true, false]) {
    await mount("#/runs", needsDaemons(writerReady));
    expect(switcher().getAttribute("aria-label")).toBe("Project: All projects, 2 waiting on you. Switch project");
    expect(badge()!.textContent).toBe("2");
    cleanup();
  }
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

test("a click outside the open switcher closes it and returns focus to the switcher button", async () => {
  await mount("#/runs");
  await key(switcher(), "ArrowDown");
  expect(document.activeElement).toBe(find());

  await userEvent.click(screen.getByRole("main"));
  await act(settle);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(switcher().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(switcher());
});

test("a click on a focusable control outside the open switcher closes it and leaves focus on that control", async () => {
  await mount("#/runs");
  await key(switcher(), "ArrowDown");
  expect(document.activeElement).toBe(find());

  const tab = within(screen.getByRole("group", { name: "Run states" })).getAllByRole("button")[0]!;
  await userEvent.click(tab);
  await act(settle);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(switcher().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(tab);
});

test("typing filters the options, ArrowDown moves through the matches and Enter picks the active one", async () => {
  await mount("#/runs", threeProjects);
  fireEvent.click(switcher());
  await act(settle);
  await type("lo");
  expect(optionNames()).toEqual(["halo", "Lotuspod"]);
  expect(activeName()).toContain("halo");

  await key(find(), "ArrowDown");
  expect(activeName()).toContain("Lotuspod");
  await key(find(), "Enter");
  expect(location()).toEqual({ path: "/runs", project: "Lotuspod" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(switcher().getAttribute("aria-label")).toBe("Project: Lotuspod. Switch project");
});

test("with the link naming a project no host serves, opening highlights All projects and Enter picks it", async () => {
  await mount("#/runs?project=ghost");
  await key(switcher(), "ArrowDown");
  expect(activeName()).toContain("All projects");
  await key(find(), "Enter");
  expect(location()).toEqual({ path: "/runs", project: null });
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("Shift+Tab from the filter closes the switcher with focus on its button", async () => {
  await mount("#/runs");
  await key(switcher(), "ArrowDown");
  await userEvent.tab({ shift: true });
  await act(settle);
  expect(document.activeElement).toBe(switcher());
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("the filter matches accents exactly, so typing cafe and Enter picks cafe over café", async () => {
  await mount("#/runs", accented);
  fireEvent.click(switcher());
  await act(settle);
  await type("cafe");
  expect(optionNames()).toEqual(["cafe"]);
  await key(find(), "Enter");
  expect(location()).toEqual({ path: "/runs", project: "cafe" });
});

test("the button holds one dot per project in scope in its status colour and no project icon: every project's in order for All projects, the chosen one's alone", async () => {
  await mount("#/runs", coloured);
  expect(dots()).toEqual(["var(--p-violet)", "var(--p-teal)"]);
  const drawn = switcher().querySelectorAll("svg");
  expect(drawn).toHaveLength(1);
  expect(switcher().lastElementChild!.contains(drawn[0]!)).toBe(true);
  cleanup();

  await mount("#/runs?project=writer", coloured);
  expect(dots()).toEqual(["var(--p-teal)"]);
});

test("folded to the rail, the button keeps the scope's dots and its name", async () => {
  await mount("#/runs", coloured);
  try {
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(screen.getByRole("complementary", { name: "Console" }).getAttribute("data-folded")).toBe("true");
    expect(dots()).toEqual(["var(--p-violet)", "var(--p-teal)"]);
    expect(switcher().querySelector("svg")).toBeNull();
    expect(switcher().getAttribute("aria-label")).toBe("Project: All projects. Switch project");
  } finally {
    localStorage.clear();
  }
});

test("each option shows its project's colour dot and name with no icon or count, and All projects one dot per project", async () => {
  await mount("#/runs", coloured);
  fireEvent.click(switcher());
  await act(settle);
  expect(optionNames()).toEqual(["All projects", "demo", "writer"]);
  expect(optionDots()).toEqual([["var(--p-violet)", "var(--p-teal)"], ["var(--p-violet)"], ["var(--p-teal)"]]);
  expect(options().map((option) => option.querySelectorAll("svg:not([data-check])").length)).toEqual([0, 0, 0]);
});

test("a held project shows held and a project whose host stopped answering draws its dot hollow and is named not answering, neither with a count", async () => {
  const hosts: Record<string, StubDaemon> = {
    [ORIGIN]: { status: { ...statusOf("/srv/dev/demo", 1, "held"), colour: "violet" }, attention: attentionOf([one], "/srv/dev/demo") },
    [PEER]: { status: { ...statusOf("/srv/dev/writer", 2, "enabled"), colour: "teal" }, attention: attentionOf([], "/srv/dev/writer") },
  };
  const queries = await mount("#/runs", peersFetch(ORIGIN, hosts));
  hosts[PEER] = { down: new Error("connection refused") };
  await act(async () => {
    await queries.refetchQueries();
    await settle();
  });
  await act(settle);

  fireEvent.click(switcher());
  await act(settle);
  expect(optionNames()).toEqual(["All projects", "demoheld", "writer not answering"]);
  expect(within(screen.getByRole("listbox", { name: "Projects" })).getByRole("option", { name: "writer not answering" })).toBe(options()[2]!);
  const drawn = (option: HTMLElement) => Array.from(option.querySelectorAll<HTMLElement>("[data-dot]"), (dot) => [dot.style.background, dot.style.boxShadow]);
  const [hollow] = drawn(options()[2]!);
  expect(hollow![0]).toBe("transparent");
  expect(hollow![1]).toContain("inset 0 0 0 1.5px var(--bad)");
  const [filled] = drawn(options()[1]!);
  expect(filled![0]).toBe("var(--p-violet)");
  expect(filled![1]).not.toContain("var(--bad)");
});
