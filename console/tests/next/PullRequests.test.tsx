import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, Status, StatusRun } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import ready from "../../../tests/fixtures/serve/run-merge-ready.json";
import ticketDetail from "../../../tests/fixtures/serve/ticket.json";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NOW = new Date(2026, 9, 15, 16, 30).getTime();

type Item = Attention["items"][number];
const park = (run: number, parkKind: string, ageMs: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run, ticket: `DEMO-${run}`, ticket_url: null, title: `Ticket ${run}`,
  park_kind: parkKind, reason: "PR open", asked_ms: NOW - ageMs,
  pr_url: `https://github.com/example/demo/pull/${run + 33}`, pr: { title: null, state: "ready", number: run + 33, ...pr },
} as Item);
const onCi = park(8, "ci", 3 * HOUR, { number: 41, checks: "pending", review: null, threads: 0, open_threads: 0 });
const readyPark = park(9, "pull_request", 2 * HOUR, { number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0 });
const threadPark = park(10, "pull_request", HOUR, { number: 99, checks: "success", review: "approved", threads: 1, open_threads: 1 });
const draftPark = park(12, "pull_request", HOUR, { number: 45, state: "draft", checks: "success", review: null, threads: 0, open_threads: 0 });
const PR_42 = "https://github.com/example/demo/pull/42";
const WHAT = "## What / Why / How\n\n**What:** Adds a slot that takes pages.\n\n**Why:** Paging.\n";

interface Daemon {
  items: Item[];
  runs?: StatusRun[];
  actions?: boolean;
  readyRuns?: number[];
  projects?: [string, string];
  ticketBody?: string;
  sendBack?: { ok: boolean; detail: string };
  markReady?: { ok: boolean; detail: string };
}

let client: QueryClient | null = null;

async function mount({ items, runs = [], actions = false, readyRuns = [9], projects = ["demo", "writer"], ticketBody = "", sendBack = { ok: true, detail: "sent back" }, markReady = { ok: true, detail: "marked ready" } }: Daemon) {
  const statusOf = (project: string, own: StatusRun[] = []): Status => ({ ...served, project, now: NOW, runs: own, queued: [], actions });
  const attention = (project: string, list: Item[]) => ({ level: list.length ? "attention" : "none", now: NOW, project, items: list });
  const [first, second] = projects.map((name) => `/srv/dev/${name}`) as [string, string];
  const fleet = peersFetch(DEMO, {
    [DEMO]: { status: statusOf(first, runs), attention: attention(first, items) },
    [WRITER]: { status: statusOf(second), attention: attention(second, []) },
  });
  const { seen, fetchImpl } = fakeFetch({});
  const routed: typeof fetchImpl = async (url, init) => {
    const merge = url.match(/\/runs\/(\d+)\/merge$/);
    if (merge) return Response.json({ ...ready, run: Number(merge[1]), ready: readyRuns.includes(Number(merge[1])) });
    const ticket = url.match(/\/tickets\/([^/?]+)$/);
    if (ticket) {
      await fetchImpl(url, init);
      return Response.json({ ...ticketDetail, ticket: decodeURIComponent(ticket[1]!), body: ticketBody });
    }
    if (url.endsWith("/actions/send-back")) {
      await fetchImpl(url, init);
      return Response.json(sendBack);
    }
    if (url.endsWith("/actions/mark-ready")) {
      await fetchImpl(url, init);
      return Response.json(markReady);
    }
    return fleet(url, init);
  };
  window.location.hash = "#/";
  const fake = fakeDeps(routed);
  fake.clock.now = NOW;
  client = consoleQueryClient();
  render(
    <QueryClientProvider client={client}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return seen;
}

function unmount() {
  cleanup();
  client?.clear();
}

const section = () => screen.getByRole("region", { name: "Pull requests" });
const lane = (name: string) => within(section()).getByRole("group", { name });
const rows = () => [...section().querySelectorAll<HTMLElement>("[data-pr-row]")];
const waits = () => rows().map((row) => [
  row.querySelector("[data-pr-wait]")!.textContent,
  row.querySelector("[data-pr-since]")?.textContent ?? "",
]);
const facts = (row: Element) => [...row.querySelectorAll("[data-fact]")].map((fact) => fact.textContent);
const rowButton = (pr: number) => within(section()).getByRole("button", { name: new RegExp(`^PR #${pr}, `) });

async function openRow(pr: number) {
  await act(async () => {
    fireEvent.click(rowButton(pr));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  unmount();
  client = null;
  window.location.hash = "";
});

test("a lane per project counts the pull requests waiting on you, longest waiting first, and with only a ci park left the section says none waits on you", async () => {
  await mount({ items: [threadPark, readyPark], projects: ["alpha", "beta"] });
  expect(lane("alpha").querySelector("[data-lane-head]")!.textContent).toContain("2 open");
  expect(within(section()).queryByRole("group", { name: "beta" })).toBeNull();
  expect(section().textContent).not.toContain("0 open");
  expect(rows().map((row) => within(row).getByRole("link", { name: /^#/ }).textContent)).toEqual(["#42", "#43"]);
  unmount();

  await mount({ items: [onCi], projects: ["alpha", "beta"] });
  expect(within(section()).queryAllByRole("group")).toEqual([]);
  expect(section().textContent).toContain("No pull request waits on you.");
});

test("only rows waiting on you stay: a draft and a ready park, not a ci park or a run fixing another pull request", async () => {
  const draft = park(12, "pull_request", 3 * HOUR, { state: "draft", checks: "success", review: "review_required", threads: 0, open_threads: 0 });
  const [base] = served.runs as [StatusRun];
  const fixing = { ...base, id: 20, ticket: "DEMO-20", title: "Fix the slot", phase: "working", heartbeat_age_ms: 1000, round: 2,
    pr_url: "https://github.com/example/demo/pull/2374" };
  await mount({ items: [draft, readyPark, onCi], runs: [fixing] });
  expect(lane("demo").querySelector("[data-lane-head]")!.textContent).toContain("2 open");
  expect(rows().map((row) => within(row).getByRole("link", { name: /^#/ }).textContent)).toEqual(["#45", "#42"]);
  expect(waits()).toEqual([["Your review", "draft · 3h"], ["Your review", "ready to merge · 2h"]]);
  expect(section().textContent).not.toMatch(/waiting on the factory|On CI|Factory fixing|#41|#2374/);
});

test("a draft held for review reads Your review over draft and its age, with no chip or repeated state", async () => {
  await mount({ items: [park(12, "pull_request", 2 * 24 * HOUR, {
    state: "draft", title: "Improve the overview", checks: "success", review: "review_required", threads: 0, open_threads: 0,
  })], readyRuns: [] });
  const row = rows()[0]!;
  expect(waits()).toEqual([["Your review", "draft · 2d"]]);
  expect(within(row).queryByText("Draft", { exact: true })).toBeNull();
  expect(row.textContent).not.toContain("held for your review");
  expect(row.textContent).not.toContain("review pending");
});

test("a row names its pull request by its bare number from its URL and draws its facts line", async () => {
  await mount({ items: [readyPark, threadPark] });
  const second = rows()[1]!;
  expect(within(second).getByRole("link", { name: "#43" })).toBeTruthy();
  expect(facts(second)).toEqual(["checks ✓", "1 open thread"]);
});

test("an opened row shows its ticket's What line and the links that move it, not the classic detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [], ticketBody: WHAT });
  await openRow(42);
  const row = rows()[0]!;
  expect(rowButton(42).getAttribute("aria-expanded")).toBe("true");
  expect(within(row).getByText("Adds a slot that takes pages.")).toBeTruthy();
  expect(within(row).getByRole("link", { name: "Open PR to review" }).getAttribute("href")).toBe(PR_42);
  expect(within(row).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/9");
  expect(within(row).queryByRole("region", { name: "Pull request facts" })).toBeNull();
  expect(row.textContent).not.toContain("Last factory activity");
  expect(within(row).queryByText(PR_42)).toBeNull();
  expect(seen.map((request) => request.url)).toEqual([`${DEMO}/tickets/DEMO-9`]);
});

test("Send back posts the typed note for the run, keeps it on a refusal, and is absent without actions", async () => {
  const row = () => rows()[0]!;
  const sendNote = async () => {
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Send back with a note" }));
      await settle();
    });
    fireEvent.change(within(row()).getByLabelText("Note for the factory"), { target: { value: "Rebase on main first" } });
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Send" }));
      await settle();
    });
  };
  const seen = await mount({ items: [readyPark], actions: true, readyRuns: [] });
  await openRow(42);
  await sendNote();
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/send-back`, body: { run: 9, note: "Rebase on main first" } }]);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Sent PR #42 back with your note.");
  unmount();

  await mount({ items: [readyPark], actions: true, readyRuns: [], sendBack: { ok: false, detail: "run 9 is not parked" } });
  await openRow(42);
  await sendNote();
  expect((within(row()).getByLabelText("Note for the factory") as HTMLTextAreaElement).value).toBe("Rebase on main first");
  expect(within(row()).getByRole("alert").textContent).toBe("run 9 is not parked");
  unmount();

  await mount({ items: [readyPark], actions: false, readyRuns: [] });
  await openRow(42);
  expect(within(row()).getByRole("link", { name: "Open run" })).toBeTruthy();
  expect(within(row()).queryByRole("button", { name: "Send back with a note" })).toBeNull();
});

test("Mark ready for review confirms, posts the run and toasts on a draft with actions; a refusal shows its detail", async () => {
  const row = () => rows()[0]!;
  const markReady = async () => {
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Mark ready for review" }));
      await settle();
    });
    const confirm = within(screen.getByRole("dialog", { name: "Mark #45 ready for review?" }));
    expect(confirm.getByText("Review bots and the full checks start on GitHub.")).toBeTruthy();
    await act(async () => {
      fireEvent.click(confirm.getByRole("button", { name: "Mark ready for review" }));
      await settle();
    });
  };
  const seen = await mount({ items: [draftPark], actions: true, readyRuns: [] });
  await openRow(45);
  const buttons = [...row().querySelectorAll("[data-pr-detail] a, [data-pr-detail] button")].map((node) => node.textContent);
  expect(buttons.slice(0, 2)).toEqual(["Open PR to review", "Mark ready for review"]);
  await markReady();
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/mark-ready`, body: { run: 12 } }]);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Marked #45 ready for review.");
  expect(screen.queryByRole("dialog")).toBeNull();
  unmount();

  await mount({ items: [draftPark], actions: true, readyRuns: [], markReady: { ok: false, detail: "run 12 is not a draft held for review" } });
  await openRow(45);
  await markReady();
  expect(within(screen.getByRole("dialog")).getByRole("alert").textContent).toBe("run 12 is not a draft held for review");
});

test("Mark ready for review is absent on a ready pull request and on a daemon without actions", async () => {
  for (const [item, actions, pr] of [[readyPark, true, 42], [draftPark, false, 45]] as const) {
    await mount({ items: [item], actions, readyRuns: [] });
    await openRow(pr);
    expect(within(rows()[0]!).getByRole("link", { name: "Open PR to review" })).toBeTruthy();
    expect(within(rows()[0]!).queryByRole("button", { name: "Mark ready for review" })).toBeNull();
    unmount();
  }
});

test("the PR number links to GitHub without toggling or fetching the row detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [] });
  const link = within(rows()[0]!).getByRole("link", { name: "#42" });
  const button = rowButton(42);
  expect(link.getAttribute("href")).toBe(PR_42);
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noreferrer");
  expect(link.closest("button")).toBeNull();
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(seen).toEqual([]);
  await act(async () => {
    fireEvent.click(button);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(seen.some(({ url }) => url === `${DEMO}/tickets/DEMO-9`)).toBe(true);
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
});

const SHOTS = ["01 split view", "02 custom slot", "03 saved document"].map((caption, index) => ({ caption, url: `https://media.example/${index + 1}.png` }));
const VIDEOS = [{ caption: "video-01.webm", url: "https://media.example/video-01.webm" }];
const TILE_NAMES = ["01 split view", "02 custom slot", "03 saved document", "Video: video-01.webm"];
const mediaPark = (media: object) => park(9, "pull_request", 2 * HOUR, {
  number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0, preview_url: null, ...media,
});
const tileNames = (row: HTMLElement) => [...row.querySelectorAll<HTMLElement>("[data-media-tile]")].map((tile) => tile.getAttribute("aria-label"));
const tile = (name: string) => within(rows()[0]!).getByRole("button", { name });
const dialog = () => screen.queryByRole("dialog");
const press = (key: string, shiftKey = false) => act(() => void fireEvent.keyDown(document.activeElement ?? document.body, { key, shiftKey }));

async function openMedia(name: string) {
  await mount({ items: [mediaPark({ screenshots: SHOTS, videos: VIDEOS })], readyRuns: [] });
  await openRow(42);
  await act(async () => {
    fireEvent.click(tile(name));
    await settle();
  });
}

test("a row reads its bare number and its key before its title, and opening it marks only its row and shows Open Vercel preview", async () => {
  await mount({ items: [onCi, park(9, "pull_request", 2 * HOUR, {
    number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0,
    changed_files: 13, additions: 718, deletions: 85, preview_url: "https://preview.example", screenshots: SHOTS,
  }), threadPark], readyRuns: [] });
  const demo9 = () => rows().find((row) => row.textContent!.includes("DEMO-9"))!;
  expect(within(demo9()).getByRole("link", { name: "#42" }).getAttribute("href")).toBe(PR_42);
  expect(rowButton(42).textContent).toStartWith("DEMO-9Ticket 9");
  expect(within(demo9()).queryByRole("button", { name: /needs you/i })).toBeNull();
  expect(rows().filter((row) => row.hasAttribute("data-open"))).toEqual([]);
  await openRow(42);
  expect(rows().filter((row) => row.hasAttribute("data-open"))).toEqual([demo9()]);
  const detail = demo9().querySelector("[data-pr-detail]")!;
  expect(within(demo9()).getByRole("link", { name: "Open Vercel preview" }).getAttribute("href")).toBe("https://preview.example");
  expect(within(demo9()).queryByRole("link", { name: "Open preview" })).toBeNull();
  expect(detail.textContent).not.toContain("13 files");
});

test("an opened row draws a tile button per screenshot, then per video, and none when it has no media", async () => {
  await mount({ items: [mediaPark({ screenshots: SHOTS, videos: VIDEOS })], readyRuns: [] });
  await openRow(42);
  expect(tileNames(rows()[0]!)).toEqual(TILE_NAMES);
  for (const name of TILE_NAMES) expect(tile(name).tagName).toBe("BUTTON");
  expect(within(rows()[0]!.querySelector("[data-media]")!).queryAllByRole("link")).toEqual([]);
  expect(within(tile("02 custom slot")).getByRole("img").getAttribute("alt")).toBe("02 custom slot");
  unmount();

  await mount({ items: [mediaPark({ screenshots: SHOTS })], readyRuns: [] });
  await openRow(42);
  expect(tileNames(rows()[0]!)).toEqual(TILE_NAMES.slice(0, 3));
  unmount();

  await mount({ items: [mediaPark({ screenshots: [] })], readyRuns: [] });
  await openRow(42);
  expect(tileNames(rows()[0]!)).toEqual([]);
  expect(within(rows()[0]!).queryByRole("link", { name: "Open Vercel preview" })).toBeNull();
});

test("a tile opens its item in a lightbox that Left and Right step through screenshots then videos, stopping at the ends", async () => {
  await openMedia("02 custom slot");
  const shown = () => screen.getByRole("dialog", { name: "Screenshot 2 of 4" });
  expect(within(shown()).getByText("2 / 4")).toBeTruthy();
  expect(within(shown()).getByText("2.png")).toBeTruthy();
  const image = within(shown()).getByRole("img");
  expect(image.getAttribute("src")).toBe(SHOTS[1]!.url);
  expect(image.getAttribute("alt")).toBe("02 custom slot");
  expect(shown().querySelector("[data-media-caption]")!.textContent).toBe("02 custom slot");
  expect(image.compareDocumentPosition(shown().querySelector("[data-media-caption]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  await press("ArrowRight");
  await press("ArrowRight");
  const last = screen.getByRole("dialog", { name: "Video 4 of 4" });
  const video = last.querySelector("video")!;
  expect(video.getAttribute("src")).toBe(VIDEOS[0]!.url);
  expect(video.hasAttribute("controls")).toBe(true);
  expect(video.hasAttribute("autoplay")).toBe(false);
  expect(video.muted).toBe(false);
  video.focus();
  expect(fireEvent.keyDown(video, { key: "Tab" })).toBe(true);
  expect((within(last).getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
  await press("ArrowRight");
  expect(dialog()!.getAttribute("aria-label")).toBe("Video 4 of 4");

  await press("ArrowLeft");
  expect(dialog()!.getAttribute("aria-label")).toBe("Screenshot 3 of 4");
  await press("ArrowLeft");
  await press("ArrowLeft");
  await press("ArrowLeft");
  expect(dialog()!.getAttribute("aria-label")).toBe("Screenshot 1 of 4");
  expect((within(dialog()!).getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
});

test("the lightbox keeps focus inside, and Esc or Close returns it to the tile of the item shown last", async () => {
  await openMedia("02 custom slot");
  await press("ArrowRight");
  expect(dialog()!.getAttribute("aria-label")).toBe("Screenshot 3 of 4");
  await press("Escape");
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(tile("03 saved document"));

  await act(async () => {
    fireEvent.click(tile("01 split view"));
    await settle();
  });
  const close = within(dialog()!).getByRole("button", { name: "Close" });
  expect(document.activeElement).toBe(close);
  await press("Tab", true);
  expect(document.activeElement).toBe(within(dialog()!).getByRole("button", { name: "Next" }));
  await press("Tab");
  expect(document.activeElement).toBe(close);
  await act(async () => {
    fireEvent.click(close);
    await settle();
  });
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(tile("01 split view"));
});

test("a poll that leaves the row no media closes the lightbox onto Open run, and returning media does not reopen it", async () => {
  const item = mediaPark({ screenshots: SHOTS, videos: VIDEOS });
  await mount({ items: [item], readyRuns: [] });
  await openRow(42);
  await act(async () => {
    fireEvent.click(tile("02 custom slot"));
    await settle();
  });
  expect(dialog()).not.toBeNull();
  const pr = item.pr as { screenshots: unknown[]; videos: unknown[] };
  const repoll = async () => {
    await act(async () => {
      await client!.refetchQueries({ queryKey: ["fleet"] });
      await settle();
    });
    for (let round = 0; round < 3; round += 1) await act(settle);
  };
  pr.screenshots = [];
  pr.videos = [];
  await repoll();
  expect(dialog()).toBeNull();
  expect(tileNames(rows()[0]!)).toEqual([]);
  expect(document.activeElement).toBe(within(rows()[0]!).getByRole("link", { name: "Open run" }));

  pr.screenshots = SHOTS;
  pr.videos = VIDEOS;
  await repoll();
  expect(tileNames(rows()[0]!)).toEqual(TILE_NAMES);
  expect(dialog()).toBeNull();
});

test("a backticked span in a row's title shows as inline code, and its accessible name drops the backticks", async () => {
  await mount({ items: [{ ...readyPark, title: "Read a `+++` TOML header" }] });
  const button = rowButton(42);
  expect(button.getAttribute("aria-label")).toStartWith("PR #42, DEMO-9 Read a +++ TOML header, ");
  expect([...button.querySelectorAll("code")].map((code) => code.textContent)).toEqual(["+++"]);
});
