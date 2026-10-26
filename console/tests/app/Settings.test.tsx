import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { readStoredScope } from "../../src/app/projects";
import { CONFIG_EDIT_OFF } from "../../src/lib/config";
import type { DayLogPrefs, LedgerEntry, Status } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import { tokenedFetch, type Fetch } from "../../src/lib/poll";
import { tokenKey } from "../../src/lib/token";
import { fakeDeps, NO_ATTENTION, peersFetch, settle, type StubDaemon } from "../harness";

const ORIGIN = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const RUNNER = "http://runner-host:7710";
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const config = (path = "/srv/demo/config.toml") => Response.json({ text: "", values: {}, path, applies: "next loop start" });

const logged = (at: number, ticket: string, kind: string, text: string): LedgerEntry => ({ at, run: 1, ticket, pr_url: null, kind, source: "loop", text });
const LEDGERS: Record<string, LedgerEntry[]> = {
  [ORIGIN]: [
    logged(3, "DEMO-1", "merge", "MERGED to main as 5acc138."),
    logged(2, "DEMO-1", "round", "Round 2: APPROVE\nReviewer verdict:\nVERDICT: APPROVE"),
    logged(1, "DEMO-1", "round", "Round 1: REQUEST_CHANGES -> fix round\nReviewer findings:\n- p1 the limit"),
  ],
  [WRITER]: [logged(4, "WRITER-1", "merge", "MERGED to main as 3c9a2f1.")],
};

function fleet(configFetch: Fetch = async () => config(), dayLog: DayLogPrefs | null = null) {
  const puts: { body: unknown; authorization: string | null }[] = [];
  const status = (project: string, host: string): Status => ({ ...working, runs: [], project: `/srv/${project}`, host, admission: project === "relos" ? "held" : "enabled" });
  const demo = status("demo", "writer-host");
  const writer = status("writer", "writer-host");
  const daemons: Record<string, StubDaemon> = {
    [ORIGIN]: { status: demo, attention: { ...NO_ATTENTION, project: demo.project } },
    [WRITER]: { status: writer, attention: { ...NO_ATTENTION, project: writer.project } },
    [RUNNER]: { status: status("relos", "runner-host"), attention: { ...NO_ATTENTION, project: "/srv/relos" } },
  };
  const poll = peersFetch(ORIGIN, daemons);
  const fetch: Fetch = async (url, init) => {
    if (url === `${ORIGIN}/prefs`) {
      if (init?.method === "PUT") puts.push({ body: JSON.parse(String(init.body)), authorization: new Headers(init.headers).get("authorization") });
      return Response.json({ day_log: dayLog, updated_ms: null });
    }
    const ledger = Object.keys(LEDGERS).find((base) => url.startsWith(`${base}/ledger?`));
    if (ledger) return Response.json({ entries: LEDGERS[ledger], active_outages: [], since: 0, limit: 1000 });
    return url.endsWith("/config") ? configFetch(url, init) : poll(url, init);
  };
  return {
    fetch,
    puts,
    failRunner: () => { daemons[RUNNER] = { down: new Error("connection refused") }; },
    staleWriter: () => {
      for (const item of [demo, writer]) item.supervisor = { ...item.supervisor, state: "stale", heartbeat_age_ms: 600000 };
    },
  };
}

async function mount(hash: string, source = fleet()) {
  window.location.hash = hash;
  const fake = fakeDeps(tokenedFetch(source.fetch));
  const client = consoleQueryClient();
  render(<QueryClientProvider client={client}><ConsoleApp base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return {
    ...source,
    poll: async () => {
      await act(async () => { await client.refetchQueries({ queryKey: ["fleet", ORIGIN] }); });
      await act(settle);
      await act(settle);
    },
  };
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/`));
afterAll(() => happyDOM.setURL("about:blank"));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
  localStorage.clear();
  sessionStorage.clear();
});

test("the footer counts failing hosts once across projects and keeps Settings outside the five page links, including folded", async () => {
  const view = await mount("#/");
  expect(screen.getByRole("link", { name: "Settings" }).querySelector("[data-badge]")).toBeNull();
  view.failRunner();
  await view.poll();
  const link = screen.getByRole("link", { name: "Settings, 1 host needs attention" });
  expect(link.getAttribute("href")).toMatch(/#\/settings\/general$/);
  expect(link.querySelector("[data-badge]")?.textContent).toBe("1");
  const pages = within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link").filter((item) => !item.closest("ul"));
  expect(pages.map((item) => item.textContent?.replace(/\d+$/, ""))).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts"]);
  expect(screen.getAllByRole("link", { name: /^Settings/ })).toHaveLength(1);
  view.staleWriter();
  await view.poll();
  expect(screen.getByRole("link", { name: "Settings, 2 hosts need attention" }).querySelector("[data-badge]")?.textContent).toBe("2");
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  expect(screen.getByRole("link", { name: "Settings, 2 hosts need attention" }).textContent).toBe("2");
});

test("Review presents the project file and ordered grouped sections with matching host badge and current links", async () => {
  const view = await mount("#/settings/review?project=demo");
  view.failRunner();
  await view.poll();
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Settings");
  expect(screen.getByText("demo · config.toml on writer-host")).toBeTruthy();
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  const links = within(nav).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual(["General", "Review", "Agents", "Capture", "Hold / release", "Raw TOML", "Hosts1", "Appearance", "Today's log"]);
  expect(nav.textContent).toMatch(/Project.*General.*Review.*Agents.*Capture.*Hold \/ release.*Raw TOML.*Factory.*Hosts.*You.*Appearance.*Today's log/);
  expect(nav.textContent).not.toContain("You · this browser");
  expect(links.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.textContent)).toEqual(["Review"]);
  expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Review");
  expect(screen.getByText("[loop], [review] and [agents] in config.toml")).toBeTruthy();
  const footer = screen.getByRole("link", { name: "Settings, 1 host needs attention" });
  expect(footer.getAttribute("aria-current")).toBe("page");
  expect(footer.getAttribute("href")).toMatch(/#\/settings\/general\?project=demo$/);
  expect(screen.getByText("project settings live in config.toml on its host; each save keeps the previous text as a backup")).toBeTruthy();
  expect(screen.getByText("appearance stays in this browser")).toBeTruthy();
});

test("the project picker preserves the current tab, remembers the choice and names held projects; an unscoped Settings uses the first without rewriting", async () => {
  const view = await mount("#/settings/review?project=demo");
  view.failRunner();
  await view.poll();
  const picker = screen.getByRole("combobox", { name: "Project" });
  expect(within(picker).getAllByRole("option").map((item) => item.textContent)).toEqual(["demo", "writer", "relos · held"]);
  fireEvent.change(picker, { target: { value: "writer" } });
  await act(settle);
  expect(window.location.hash).toBe("#/settings/review?project=writer");
  expect(readStoredScope()).toBe("writer");
  expect(screen.getByText("writer · config.toml on writer-host")).toBeTruthy();
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "relos" } });
  await act(settle);
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  expect(within(nav).getByRole("link", { name: /Hold \/ release/ }).textContent).toContain("held");
  cleanup();
  await mount("#/settings");
  expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("General");
  expect((screen.getByRole("combobox", { name: "Project" }) as HTMLSelectElement).value).toBe("demo");
  expect(window.location.hash).toBe("#/settings");
});

test("project panels distinguish pending, disabled and failed config reads while Hold / release retains its heading", async () => {
  const cases: [Fetch, string][] = [
    [() => new Promise(() => {}), "Reading config.toml for demo from writer-host…"],
    [async () => new Response("not found", { status: 404 }), CONFIG_EDIT_OFF],
    [async () => { throw new Error("connection refused"); }, "writer-host did not answer /config: connection refused"],
  ];
  for (const [fetch, message] of cases) {
    await mount("#/settings/general?project=demo", fleet(fetch));
    expect(screen.getByText(message)).toBeTruthy();
    fireEvent.click(within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name: "Hold / release" }));
    await act(settle);
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Hold / release");
    cleanup();
  }
});

test("an unknown project's panels report no matching host and selecting a known project starts a config read", async () => {
  await mount("#/settings/general?project=unknown", fleet(() => new Promise(() => {})));
  for (const label of ["General", "Review", "Agents", "Capture", "Hold / release", "Raw TOML"]) {
    fireEvent.click(within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name: label }));
    await act(settle);
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(label);
    expect(screen.getByText("No host is available for unknown.")).toBeTruthy();
    expect(screen.queryByText(/^Reading config.toml/)).toBeNull();
  }
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "demo" } });
  await act(settle);
  expect(screen.getByText("Reading config.toml for demo from writer-host…")).toBeTruthy();
  expect(screen.queryByText("No host is available for unknown.")).toBeNull();
});

test("the head and project subtitles use the filename returned by config and factory and browser tabs have their own panels", async () => {
  await mount("#/settings/general?project=demo", fleet(async () => config("/srv/demo/custom.toml")));
  expect(screen.getByText("demo · custom.toml on writer-host")).toBeTruthy();
  expect(screen.getByText("[merge] and [loop] in custom.toml")).toBeTruthy();
  for (const [label, subtitle] of [["Hosts", "the factory's hosts"], ["Appearance", "yours, in this browser · applies at once, nothing to save"]] as const) {
    fireEvent.click(within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name: label }));
    await act(settle);
    expect(screen.getByRole("heading", { level: 2, name: label })).toBeTruthy();
    expect(screen.getByText(subtitle)).toBeTruthy();
    if (label === "Hosts") expect(screen.getAllByRole("article")).toHaveLength(3);
    else expect(screen.getByRole("radiogroup", { name: "Theme" })).toBeTruthy();
  }
});

test("an unknown Settings tab shows the console not-found page", async () => {
  await mount("#/settings/nope");
  expect(screen.getByText("There is no page here.")).toBeTruthy();
  expect(screen.queryByRole("navigation", { name: "Settings sections" })).toBeNull();
});

const GROUPS = ["Outcomes", "Needs you", "Operator actions", "Review rounds", "Babysit and CI", "Supervisor and system"];
const box = (name: string) => screen.getByRole("checkbox", { name }) as HTMLInputElement;
const pressed = () => within(screen.getByRole("group", { name: "Preset" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent);
const ticked = () => screen.getAllByRole("checkbox").filter((input) => (input as HTMLInputElement).checked).map((input) => input.getAttribute("aria-label"));
const HIGHLIGHT_TICKS = ["Merged", "Failed", "Needs you", "Parked", "Merge requeued", "Operator actions", "Requeues", "Steers", "Notes", "Approvals", "Pause, abort, start", "Factory amendments", "demo", "writer", "relos"];
const logPhrases = () => Array.from(screen.getByRole("complementary", { name: "Today's log" }).querySelectorAll("li"), (row) => {
  const key = row.querySelector('a[href*="/tickets/"]')?.textContent;
  return key ? `${key} ${row.querySelector("[data-phrase]")?.textContent}` : null;
}).filter(Boolean);
const toOverview = async () => {
  await act(async () => within(screen.getByRole("navigation", { name: "Pages" })).getByRole("link", { name: "Overview" }).click());
  await act(settle);
  await act(settle);
};
const click = async (element: HTMLElement) => {
  await act(async () => fireEvent.click(element));
  await act(settle);
};

test("Today's log is the second You tab, with the Preset control, one section per group in one container and a Projects section", async () => {
  await mount("#/settings/log");
  await act(settle);
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  expect(within(nav).getByRole("link", { name: "Today's log" }).getAttribute("href")).toMatch(/#\/settings\/log$/);
  expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Today's log");
  expect(screen.getByText("yours, in this browser · applies at once, nothing to save")).toBeTruthy();
  expect(within(screen.getByRole("group", { name: "Preset" })).getAllByRole("button").map((button) => button.textContent)).toEqual(["Highlights", "Everything", "Manual"]);
  expect(pressed()).toEqual(["Highlights"]);
  const sections = screen.getAllByRole("region").filter((region) => [...GROUPS, "Projects"].includes(region.getAttribute("aria-label") ?? ""));
  expect(sections.map((section) => section.getAttribute("aria-label"))).toEqual([...GROUPS, "Projects"]);
  expect(new Set(sections.map((section) => section.parentElement)).size).toBe(1);
  expect(sections[0]!.parentElement!.className).toEqual(expect.stringContaining("border border-rule bg-paper"));
  expect(within(sections[0]!).getAllByRole("checkbox").map((input) => input.getAttribute("aria-label"))).toEqual(["Outcomes", "Merged", "Failed", "PR opened"]);
  expect(within(sections.at(-1)!).getAllByRole("checkbox").map((input) => input.getAttribute("aria-label"))).toEqual(["demo", "writer", "relos"]);
  expect(box("PR opened").checked).toBe(false);
  expect([box("Outcomes").indeterminate, box("Outcomes").checked]).toEqual([true, false]);
  expect(ticked()).toEqual(HIGHLIGHT_TICKS);
});

test("ticking Verdicts selects Manual and adds the verdicts to the log; a cleared project leaves it; the presets set the boxes", async () => {
  await mount("#/settings/log");
  await act(settle);
  await click(box("Verdicts"));
  expect(pressed()).toEqual(["Manual"]);
  expect(box("Review rounds").indeterminate).toBe(true);
  await toOverview();
  expect(logPhrases()).toEqual(["WRITER-1 merged", "DEMO-1 merged", "DEMO-1 review round 2: approve", "DEMO-1 review round 1: changes asked"]);

  await click(screen.getByRole("link", { name: "Filter Today's log" }));
  await click(box("writer"));
  await toOverview();
  expect(logPhrases()).toEqual(["DEMO-1 merged", "DEMO-1 review round 2: approve", "DEMO-1 review round 1: changes asked"]);

  await click(screen.getByRole("link", { name: "Filter Today's log" }));
  const before = ticked();
  await click(screen.getByRole("button", { name: "Manual" }));
  expect(ticked()).toEqual(before);
  await click(screen.getByRole("button", { name: "Everything" }));
  expect(screen.getAllByRole("checkbox").every((input) => (input as HTMLInputElement).checked)).toBe(true);
  await toOverview();
  expect(logPhrases()).toEqual(["WRITER-1 merged", "DEMO-1 merged", "DEMO-1 review round 2: approve", "DEMO-1 review round 1: changes asked"]);
  expect(screen.queryByRole("button", { name: /^(Show all|Highlights only)/ })).toBeNull();
  await click(screen.getByRole("link", { name: "Filter Today's log" }));
  await click(screen.getByRole("button", { name: "Highlights" }));
  expect(ticked()).toEqual(HIGHLIGHT_TICKS);
});

test("a tick sends one PUT /prefs with the page's bearer, and a page reading that filter back shows Manual and the verdicts", async () => {
  localStorage.setItem(tokenKey(addressOf(ORIGIN)), "machine-token");
  const view = await mount("#/settings/log");
  await act(settle);
  await click(box("Verdicts"));
  expect(view.puts).toHaveLength(1);
  expect(view.puts[0]!.authorization).toBe("Bearer machine-token");
  const saved = (view.puts[0]!.body as { day_log: DayLogPrefs }).day_log;
  expect([saved.preset, saved.items.verdicts]).toEqual(["manual", true]);
  cleanup();

  await mount("#/settings/log", fleet(undefined, { preset: "manual", items: { verdicts: true }, projects: {} }));
  await act(settle);
  expect(pressed()).toEqual(["Manual"]);
  expect([box("Verdicts").checked, box("Merged").checked, box("PR opened").checked]).toEqual([true, true, false]);
});
