import { afterAll, afterEach, beforeAll, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardStory, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;

const story = (ticket: string, title: string, merged: number, total: number): BoardStory => ({ ticket, title, children: [], merged, total });
const demoStories = [story("DEMO-30", "Stories on the board", 0, 2), story("DEMO-12", "Unattended runs recover themselves", 2, 6)];
const writerStories = [story("WRITER-5", "Drafts keep their place", 1, 3)];

/** `demo` on DEMO and `writer` on WRITER, each `/board` listing its stories. */
function fleet(demo: BoardStory[] = demoStories, writer: BoardStory[] = writerStories): Fetch {
  const project = (name: string) => ({ ...working, project: `/srv/dev/${name}`, runs: [], queued: [], now: NOW });
  const quiet = (name: string) => ({ level: "none" as const, now: NOW, project: `/srv/dev/${name}`, items: [] });
  const polls = peersFetch(DEMO, {
    [DEMO]: { status: project("demo"), attention: quiet("demo") },
    [WRITER]: { status: project("writer"), attention: quiet("writer") },
  });
  return async (url) => {
    if (url === `${DEMO}/board`) return Response.json({ ...pinned, stories: demo });
    if (url === `${WRITER}/board`) return Response.json({ ...pinned, stories: writer });
    return polls(url);
  };
}

async function mount(hash: string, fetchImpl: Fetch = fleet()) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const storyList = () => screen.queryByRole("list", { name: "Stories" });
const listedNames = () => within(storyList()!).getAllByRole("link").map((link) => link.getAttribute("aria-label"));
const chevron = () => screen.getByRole("button", { name: /the story list$/ });

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  localStorage.clear();
  window.location.hash = "";
});

test("the Stories list under Tickets holds every project's stories by key, narrowed by the picked project", async () => {
  await mount("#/");
  expect(listedNames()).toEqual([
    "DEMO-12 Unattended runs recover themselves, 2 of 6 merged",
    "DEMO-30 Stories on the board, 0 of 2 merged",
    "WRITER-5 Drafts keep their place, 1 of 3 merged",
  ]);
  const first = screen.getByRole("link", { name: "DEMO-12 Unattended runs recover themselves, 2 of 6 merged" });
  for (const part of ["DEMO-12", "2/6", "Unattended runs recover themselves"]) expect(within(first).getByText(part)).toBeTruthy();
  cleanup();

  await mount("#/?project=writer");
  expect(listedNames()).toEqual(["WRITER-5 Drafts keep their place, 1 of 3 merged"]);
});

test("clicking a story opens its page with the scope kept, marking it and the Tickets entry current", async () => {
  await mount("#/tickets?project=demo");
  const item = screen.getByRole("link", { name: /^DEMO-12 / });
  fireEvent.click(item);
  await act(settle);
  expect(window.location.hash).toBe("#/stories/demo/DEMO-12?project=demo");
  expect(screen.getByRole("link", { name: /^DEMO-12 / }).getAttribute("aria-current")).toBe("page");
  expect(screen.getByRole("link", { name: /^DEMO-30 / }).getAttribute("aria-current")).toBeNull();
  const pages = within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link");
  const current = pages.filter((link) => link.getAttribute("aria-current") === "page" && !link.closest("ul"));
  expect(current.map((link) => link.getAttribute("aria-label"))).toEqual(["Tickets"]);
});

test("the chevron folds the list, the fold survives a remount and holds with storage throwing; a folded rail shows no list", async () => {
  await mount("#/");
  expect(chevron().getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(chevron());
  expect(chevron().getAttribute("aria-expanded")).toBe("false");
  expect(storyList()).toBeNull();
  cleanup();

  await mount("#/");
  expect(chevron().getAttribute("aria-expanded")).toBe("false");
  expect(storyList()).toBeNull();
  cleanup();
  localStorage.clear();

  const reads = spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("storage disabled");
  });
  const writes = spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("storage disabled");
  });
  try {
    await mount("#/");
    fireEvent.click(chevron());
    expect(chevron().getAttribute("aria-expanded")).toBe("false");
    expect(storyList()).toBeNull();
  } finally {
    reads.mockRestore();
    writes.mockRestore();
  }
  cleanup();

  await mount("#/");
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  expect(storyList()).toBeNull();
  expect(screen.queryByRole("button", { name: /the story list$/ })).toBeNull();
});

test("#/stories opens the first listed story, or says there are none in the scope", async () => {
  await mount("#/stories");
  expect(window.location.hash).toBe("#/stories/demo/DEMO-12");
  cleanup();

  await mount("#/stories?project=demo", fleet([], []));
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("No stories in demo");
  expect(screen.getByRole("link", { name: "Show all projects" }).getAttribute("href")).toEndWith("#/stories");
  cleanup();

  await mount("#/stories", fleet([], []));
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("No stories in all projects");
  expect(screen.queryByRole("link", { name: "Show all projects" })).toBeNull();
  expect(storyList()).toBeNull();
});
