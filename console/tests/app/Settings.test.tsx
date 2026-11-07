import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { readStoredScope } from "../../src/app/projects";
import { CONFIG_EDIT_OFF, PATH_MATCH_DEBOUNCE_MS } from "../../src/lib/config";
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
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Review");
  expect(screen.getByText("demo · config.toml on writer-host")).toBeTruthy();
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  const links = within(nav).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual(["Appearance", "Today's log", "Hosts1", "General", "Review", "Agents", "Capture", "Hold / release", "Raw TOML"]);
  expect(nav.textContent).not.toContain("You · this browser");
  expect(links.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.textContent)).toEqual(["Review"]);
  expect(screen.getByText("[loop], [review] and [agents] in config.toml")).toBeTruthy();
  const footer = screen.getByRole("link", { name: "Settings, 1 host needs attention" });
  expect(footer.getAttribute("aria-current")).toBe("page");
  expect(footer.getAttribute("href")).toMatch(/#\/settings\/general\?project=demo$/);
  expect(screen.getByText("project settings live in config.toml on its host; each save keeps the previous text as a backup")).toBeTruthy();
  expect(screen.queryByText("appearance stays in this browser")).toBeNull();
});

test("the footer names the browser only on Appearance", async () => {
  await mount("#/settings/appearance");
  const footer = screen.getByText("appearance stays in this browser").closest("footer")!;
  expect(footer.textContent).toBe("appearance stays in this browser");
});

test("the footer leaves the project settings line off the Hosts and Today's log tabs", async () => {
  for (const hash of ["#/settings/hosts", "#/settings/log"]) {
    await mount(hash);
    expect(screen.queryByText(/project settings live in/)).toBeNull();
    expect(screen.queryByText("appearance stays in this browser")).toBeNull();
    cleanup();
  }
});

const AGENTS = { agents: { implementer: { command: "claude --model opus -p", profile: "opus", fallback: "codex exec" }, adjudicator: "claude -p" } };
const agentsConfig = async () => Response.json({ text: "", values: AGENTS, path: "/srv/demo/config.toml", applies: "next loop start", revision: "a".repeat(64) });
const valueSide = (name: string) => screen.getByRole("group", { name }).children[1] as HTMLElement;

test("Agents rows share one fixed-width value column with each route line directly under its field", async () => {
  await mount("#/settings/agents?project=demo", fleet(agentsConfig));
  const routes = { Implementer: "runs claude opus", Reviewer: "runs codex gpt-6-astra", Adjudicator: "runs codex gpt-6-astra" };
  const widths = Object.keys(routes).map((name) => valueSide(name).className.split(" ").filter((name) => name.startsWith("min-[860px]:w-")));
  expect(widths[0]).toEqual(["min-[860px]:w-80"]);
  expect(widths.slice(1)).toEqual([widths[0]!, widths[0]!]);
  for (const [name, route] of Object.entries(routes)) {
    const [field, status, ...rest] = Array.from(valueSide(name).children);
    expect(rest).toHaveLength(0);
    expect(field!.matches("input, pre") || field!.querySelector("pre") !== null).toBe(true);
    expect(within(status as HTMLElement).getByText(route)).toBeTruthy();
    expect(screen.getByRole("group", { name }).querySelector("p.w-full")).toBeNull();
  }
});

const COUNTS: Record<string, number> = { "console/src/*": 214, "nope/*": 0, "console/build.ts": 1 };

function pathMatchesFleet() {
  const asked: { key: string | null; entries: string[] }[] = [];
  const source = fleet(async () => Response.json({ text: "", values: { merge: { ui_paths: ["console/src/*", "nope/*"] } }, path: "/srv/demo/config.toml", applies: "next loop start", revision: "a".repeat(64) }));
  const fetch: Fetch = async (url, init) => {
    if (!url.startsWith(`${ORIGIN}/config/path-matches?`)) return source.fetch(url, init);
    const query = new URL(url).searchParams;
    const entries = query.getAll("entry");
    asked.push({ key: query.get("key"), entries });
    return Response.json({ ref: "main", sha: "0123456789ab", matches: entries.map((entry) => ({ entry, count: COUNTS[entry] ?? 0 })) });
  };
  return { source: { ...source, fetch }, asked };
}

const matchLines = () => within(screen.getByRole("list", { name: "UI paths matches" })).getAllByRole("listitem");

test("each UI paths line is followed by its count on main, a line matching nothing in the danger ink, and an edit asks again after the debounce", async () => {
  const { source, asked } = pathMatchesFleet();
  await mount("#/settings/capture?project=demo", source);
  await act(settle);
  const area = screen.getByRole("textbox", { name: "UI paths" });
  expect(area.nextElementSibling).toBe(screen.getByRole("list", { name: "UI paths matches" }));
  expect(matchLines().map((line) => line.textContent)).toEqual(["console/src/* · 214 files", "nope/* · matches nothing"]);
  expect(matchLines().map((line) => line.classList.contains("text-bad"))).toEqual([false, true]);
  expect(asked).toEqual([{ key: "merge.ui_paths", entries: ["console/src/*", "nope/*"] }]);
  fireEvent.change(area, { target: { value: "console/src/*\nconsole/build.ts" } });
  await act(settle);
  expect(asked).toHaveLength(1);
  await act(() => new Promise((resolve) => setTimeout(resolve, PATH_MATCH_DEBOUNCE_MS + 50)));
  await act(settle);
  expect(asked.at(-1)).toEqual({ key: "merge.ui_paths", entries: ["console/src/*", "console/build.ts"] });
  expect(matchLines().map((line) => line.textContent)).toEqual(["console/src/* · 214 files", "console/build.ts · 1 file"]);
});

const TREE = {
  ref: "main", sha: "0123456789ab", files: 4,
  dirs: [
    { path: "", files: 4, dirs: ["console", "docs"], leaves: [] },
    { path: "console", files: 3, dirs: ["src"], leaves: ["build.ts"] },
    { path: "console/src", files: 2, dirs: [], leaves: ["a.ts", "b.ts"] },
    { path: "docs", files: 1, dirs: [], leaves: ["x.md"] },
  ],
};

function treeFleet(uiPaths: string[], tree: () => Response = () => Response.json(TREE)) {
  const source = fleet(async () => Response.json({ text: "", values: { merge: { ui_paths: uiPaths } }, path: "/srv/demo/config.toml", applies: "next loop start", revision: "a".repeat(64) }));
  const fetch: Fetch = async (url, init) => {
    if (url === `${ORIGIN}/config/tracked-tree`) return tree();
    if (!url.startsWith(`${ORIGIN}/config/path-matches?`)) return source.fetch(url, init);
    const entries = new URL(url).searchParams.getAll("entry");
    return Response.json({ ref: "main", sha: "0123456789ab", matches: entries.map((entry) => ({ entry, count: COUNTS[entry] ?? 0 })) });
  };
  return { ...source, fetch };
}

async function mountTree(source: ReturnType<typeof treeFleet>) {
  await mount("#/settings/capture?project=demo", source);
  await act(settle);
}

const pick = (name: string) => fireEvent.click(within(screen.getByRole("group", { name: "UI paths view" })).getByRole("button", { name }));
const treeBox = (name: string) => screen.getByRole("checkbox", { name });
const patterns = () => within(screen.getByRole("list", { name: "UI paths patterns" })).getAllByRole("listitem").map((item) => item.textContent);

test("in Tree view checking console/src/ then console/build.ts writes those two lines with console/ mixed and 3 of 4 covered, and checking console/ folds them to console/*", async () => {
  await mountTree(treeFleet([]));
  pick("Tree");
  expect(screen.queryByRole("textbox", { name: "UI paths" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Open console/" }));
  fireEvent.click(treeBox("console/src/"));
  fireEvent.click(treeBox("console/build.ts"));
  expect(patterns()).toEqual(["console/src/* · 2 files", "console/build.ts · 1 file"]);
  expect(treeBox("console/").getAttribute("aria-checked")).toBe("mixed");
  expect(treeBox("docs/").getAttribute("aria-checked")).toBe("false");
  expect(screen.getByLabelText("UI paths covered").textContent).toBe("3 of 4 tracked files");
  pick("Text");
  expect((screen.getByRole("textbox", { name: "UI paths" }) as HTMLTextAreaElement).value).toBe("console/src/*\nconsole/build.ts");
  pick("Tree");
  fireEvent.click(treeBox("console/"));
  expect(patterns()).toEqual(["console/* · 3 files"]);
  expect(treeBox("console/").getAttribute("aria-checked")).toBe("true");
});

test("a pattern the tree cannot show is kept from text while checking docs/ adds docs/*", async () => {
  await mountTree(treeFleet(["console/src/*", "*.css"]));
  pick("Tree");
  expect(patterns()).toEqual(["console/src/* · 2 files", "*.css · kept from text"]);
  fireEvent.click(treeBox("docs/"));
  expect(patterns()).toEqual(["console/src/* · 2 files", "*.css · kept from text", "docs/* · 1 file"]);
  pick("Text");
  expect((screen.getByRole("textbox", { name: "UI paths" }) as HTMLTextAreaElement).value).toBe("console/src/*\n*.css\ndocs/*");
});

test("a pattern no regular expression can hold, such as a reversed range, still shows the tree and is kept from text", async () => {
  await mountTree(treeFleet(["[z-a].ts"]));
  pick("Tree");
  expect(patterns()).toEqual(["[z-a].ts · kept from text"]);
  expect(treeBox("console/").getAttribute("aria-checked")).toBe("false");
});

test("unchecking one file under a checked folder writes its remaining siblings in place of the folder", async () => {
  await mountTree(treeFleet(["console/*"]));
  pick("Tree");
  fireEvent.click(screen.getByRole("button", { name: "Open console/" }));
  fireEvent.click(screen.getByRole("button", { name: "Open console/src/" }));
  fireEvent.click(treeBox("console/src/a.ts"));
  expect(patterns()).toEqual(["console/src/b.ts · 1 file", "console/build.ts · 1 file"]);
  expect(treeBox("console/").getAttribute("aria-checked")).toBe("mixed");
  expect(screen.getByLabelText("UI paths covered").textContent).toBe("2 of 4 tracked files");
});

test("switching Tree to Text shows the same lines with their counts on main, the view is remembered, and a 413 tree offers Text only", async () => {
  await mountTree(treeFleet(["console/src/*", "console/build.ts"]));
  pick("Tree");
  expect(screen.queryByRole("list", { name: "UI paths matches" })).toBeNull();
  pick("Text");
  await act(settle);
  expect((screen.getByRole("textbox", { name: "UI paths" }) as HTMLTextAreaElement).value).toBe("console/src/*\nconsole/build.ts");
  expect(matchLines().map((line) => line.textContent)).toEqual(["console/src/* · 214 files", "console/build.ts · 1 file"]);
  pick("Tree");
  cleanup();
  await mountTree(treeFleet(["console/src/*"]));
  expect(screen.getByRole("button", { name: "Tree" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("list", { name: "UI paths tree" })).toBeTruthy();
  cleanup();
  await mountTree(treeFleet(["console/src/*"], () => Response.json({ error: "main tracks 20001 files", files: 20001, limit: 20000 }, { status: 413 })));
  expect(screen.queryByRole("group", { name: "UI paths view" })).toBeNull();
  expect(screen.queryByRole("list", { name: "UI paths tree" })).toBeNull();
  expect((screen.getByRole("textbox", { name: "UI paths" }) as HTMLTextAreaElement).value).toBe("console/src/*");
});

test("an implementer inline table shows read-only in full in a pre with a link to Raw TOML, and an absent reviewer shows no default tag", async () => {
  await mount("#/settings/agents?project=demo", fleet(agentsConfig));
  const implementer = within(screen.getByRole("group", { name: "Implementer" }));
  expect(JSON.parse(implementer.getByLabelText("Implementer").textContent!)).toEqual(AGENTS.agents.implementer);
  expect(implementer.getByLabelText("Implementer").tagName).toBe("PRE");
  expect(screen.getByRole("group", { name: "Implementer" }).querySelector("input, textarea")).toBeNull();
  expect((implementer.getByRole("link", { name: "Edit in Raw TOML" }) as HTMLAnchorElement).hash).toBe("#/settings/toml?project=demo");
  const reviewer = within(screen.getByRole("group", { name: "Reviewer" }));
  expect((reviewer.getByRole("textbox", { name: "Reviewer" }) as HTMLInputElement).placeholder).toBe("the default route");
  expect(reviewer.queryByText("default")).toBeNull();
});

test("the sidebar's project picker preserves the current tab, remembers the choice and names held projects; an unscoped Settings uses the first without rewriting", async () => {
  const view = await mount("#/settings/general?project=demo");
  view.failRunner();
  await view.poll();
  const sidebar = screen.getByRole("complementary", { name: "Console" });
  const picker = within(sidebar).getByRole("combobox", { name: "Project" }) as HTMLSelectElement;
  expect(within(picker).getAllByRole("option").map((item) => item.textContent)).toEqual(["demo", "writer", "relos · held"]);
  fireEvent.change(picker, { target: { value: "writer" } });
  await act(settle);
  expect(window.location.hash).toBe("#/settings/general?project=writer");
  expect(readStoredScope()).toBe("writer");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("General");
  expect(screen.getByText("writer · config.toml on writer-host")).toBeTruthy();
  expect((within(sidebar).getByRole("combobox", { name: "Project" }) as HTMLSelectElement).value).toBe("writer");
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "relos" } });
  await act(settle);
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  expect(within(nav).getByRole("link", { name: /Hold \/ release/ }).textContent).toContain("held");
  cleanup();
  await mount("#/settings");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("General");
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
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Hold / release");
    cleanup();
  }
});

test("an unknown project's panels report no matching host and selecting a known project starts a config read", async () => {
  await mount("#/settings/general?project=unknown", fleet(() => new Promise(() => {})));
  for (const label of ["General", "Review", "Agents", "Capture", "Hold / release", "Raw TOML"]) {
    fireEvent.click(within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name: label }));
    await act(settle);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(label);
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
    expect(screen.getByRole("heading", { level: 1, name: label })).toBeTruthy();
    expect(screen.getByText(subtitle)).toBeTruthy();
    if (label === "Hosts") expect(screen.getAllByRole("article")).toHaveLength(3);
    else expect(screen.getByRole("radiogroup", { name: "Theme" })).toBeTruthy();
  }
});

test("Raw TOML drops the page's width cap and sizes its editor to the viewport with a 360px floor, while General keeps the cap", async () => {
  const toml = async () => Response.json({ text: "[loop]\nworkers = 2\n", values: {}, path: "/srv/demo/config.toml", applies: "next loop start", revision: "3f9a2c1b" + "a".repeat(56), saved: null, modified_ms: null });
  await mount("#/settings/toml?project=demo", fleet(toml));
  const classes = (element: Element) => element.className.split(/\s+/);
  expect(classes(screen.getByRole("region", { name: "Raw TOML" }))).not.toContain("max-w-[1180px]");
  const scroller = classes(screen.getByRole("textbox", { name: "config.toml for demo" }).closest("[data-toml-scroller]")!);
  expect(scroller).not.toContain("h-[360px]");
  expect(scroller.filter((name) => /^h-\[max\(360px,calc\(100dvh-\d+px\)\)\]$/.test(name))).toHaveLength(1);
  fireEvent.click(within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name: "General" }));
  await act(settle);
  expect(classes(screen.getByRole("region", { name: "General" }))).toContain("max-w-[1180px]");
});

test("on Appearance the sidebar holds Back and the grouped sections in place of the switcher and Pages, and the page holds only that section", async () => {
  await mount("#/settings/appearance");
  const sidebar = screen.getByRole("complementary", { name: "Console" });
  expect(within(sidebar).getByRole("link", { name: "Back" })).toBeTruthy();
  expect(within(sidebar).queryByRole("navigation", { name: "Pages" })).toBeNull();
  expect(within(sidebar).queryByRole("button", { name: /Switch project$/ })).toBeNull();
  const nav = within(sidebar).getByRole("navigation", { name: "Settings sections" });
  const groups = within(nav).getAllByRole("group").map((group) => [
    document.getElementById(group.getAttribute("aria-labelledby")!)?.textContent,
    within(group).getAllByRole("link").map((link) => link.textContent),
  ]);
  expect(groups).toEqual([
    ["You", ["Appearance", "Today's log"]],
    ["Factory", ["Hosts"]],
    ["Project", ["General", "Review", "Agents", "Capture", "Hold / release", "Raw TOML"]],
  ]);
  const footer = within(sidebar).getByRole("contentinfo");
  expect(within(footer).getByRole("link", { name: "Settings" })).toBeTruthy();
  expect(within(within(footer).getByRole("group", { name: "Theme" })).getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual(["Dawn", "Night", "System"]);

  const current = within(nav).getByRole("link", { name: "Appearance" });
  expect(current.getAttribute("aria-current")).toBe("page");
  expect(current.className).toEqual(expect.stringContaining("border-act bg-act-wash text-act"));
  expect(screen.getAllByRole("navigation", { name: "Settings sections" })).toHaveLength(1);
  expect(screen.getAllByRole("combobox", { name: "Project" }).every((select) => sidebar.contains(select))).toBe(true);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Appearance");
});

test("Back links to the page Settings was opened from, with its search, after moving between sections and projects", async () => {
  await mount("#/runs?project=demo&state=failed");
  await act(async () => fireEvent.click(screen.getByRole("link", { name: "Settings" })));
  await act(settle);
  await act(async () => fireEvent.click(within(screen.getByRole("navigation", { name: "Settings sections" })).getByRole("link", { name: "Review" })));
  await act(settle);
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "writer" } });
  await act(settle);
  expect(window.location.hash).toBe("#/settings/review?project=writer");
  const back = screen.getByRole("link", { name: "Back" });
  expect(back.getAttribute("href")).toMatch(/#\/runs\?project=demo&state=failed$/);
  await act(async () => fireEvent.click(back));
  await act(settle);
  expect(window.location.hash).toBe("#/runs?project=demo&state=failed");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Runs");
});

test("Settings as the first page loaded goes Back to the Overview in its scope", async () => {
  await mount("#/settings/general?project=demo");
  const back = screen.getByRole("link", { name: "Back" });
  expect(back.getAttribute("href")).toMatch(/#\/\?project=demo$/);
  await act(async () => fireEvent.click(back));
  await act(settle);
  expect(window.location.hash).toBe("#/?project=demo");
  expect(within(screen.getByRole("navigation", { name: "Pages" })).getByRole("link", { name: /^Overview/ }).getAttribute("aria-current")).toBe("page");
});

test("a folded sidebar draws unfolded without its fold button on Settings, and folds again on Back without rewriting the stored fold", async () => {
  localStorage.setItem("holophyte.next.rail", "folded");
  await mount("#/");
  const sidebar = () => screen.getByRole("complementary", { name: "Console" });
  expect(sidebar().getAttribute("data-folded")).toBe("true");
  await act(async () => fireEvent.click(screen.getByRole("link", { name: "Settings" })));
  await act(settle);
  expect(sidebar().getAttribute("data-folded")).toBe("false");
  expect(screen.queryByRole("button", { name: /^(Expand|Collapse) sidebar$/ })).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole("link", { name: "Back" })));
  await act(settle);
  expect(window.location.hash).toBe("#/");
  expect(sidebar().getAttribute("data-folded")).toBe("true");
  expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeTruthy();
  expect(localStorage.getItem("holophyte.next.rail")).toBe("folded");
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
const preview = () => screen.getByRole("complementary", { name: "Today's log preview" });
const previewPhrases = () => Array.from(preview().querySelectorAll("li"), (row) => row.querySelector("[data-phrase]")?.textContent).filter(Boolean);
const toOverview = async () => {
  await act(async () => screen.getByRole("link", { name: "Back" }).click());
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
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Today's log");
  expect(screen.getByText("yours, in this browser · applies at once, nothing to save")).toBeTruthy();
  expect(within(screen.getByRole("group", { name: "Preset" })).getAllByRole("button").map((button) => button.textContent)).toEqual(["Highlights", "Everything", "Manual"]);
  expect(pressed()).toEqual(["Highlights"]);
  const header = screen.getByRole("group", { name: "Preset" }).closest("header")!;
  expect(header.contains(screen.getByRole("heading", { level: 1, name: "Today's log" }))).toBe(true);
  expect(header.className).toEqual(expect.stringContaining("border-b border-rule"));
  expect(header.parentElement!.className).not.toContain("bg-paper");
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

test("ticking Verdicts selects Manual and adds the verdicts to the preview and the log; a cleared project leaves it; the presets set the boxes", async () => {
  await mount("#/settings/log");
  await act(settle);
  expect(previewPhrases()).toEqual(["merged", "merged"]);
  expect(within(preview()).getByText("2 of 4")).toBeTruthy();
  await click(box("Verdicts"));
  expect(previewPhrases()).toEqual(["merged", "merged", "review round 2: approve", "review round 1: changes asked"]);
  expect(within(preview()).getByText("4 of 4")).toBeTruthy();
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

test("a tick on a saved preset keeps the projects it hides, on the page and in the PUT", async () => {
  for (const preset of ["highlights", "everything"] as const) {
    const view = await mount("#/settings/log", fleet(undefined, { preset, items: {}, projects: { writer: false } }));
    await act(settle);
    await click(box("Verdicts"));
    expect(box("writer").checked).toBe(false);
    expect((view.puts.at(-1)!.body as { day_log: DayLogPrefs }).day_log.projects).toEqual({ writer: false });
    expect(within(preview()).queryByRole("link", { name: "WRITER-1" })).toBeNull();
    cleanup();
  }
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
  cleanup();

  await mount("#/settings/log", fleet(undefined, { preset: "everything", items: { merged: true }, projects: {} }));
  await act(settle);
  expect(pressed()).toEqual(["Everything"]);
  expect([box("Merged").checked, box("Verdicts").checked, box("PR opened").checked, box("Parked").checked]).toEqual([true, false, false, true]);
  expect(previewPhrases()).toEqual(["merged", "merged"]);
});
