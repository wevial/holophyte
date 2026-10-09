import { afterEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AskAction } from "../src/components/AskAction";
import { addressOf } from "../src/lib/hosts";
import { tokenedFetch } from "../src/lib/poll";
import { storeToken } from "../src/lib/token";
import pinned from "../../tests/fixtures/serve/run-asks.json";
import { fakeFetch } from "./actionFakes";
import { settle } from "./harness";

const base = "http://writer:7710";
const prUrl = "https://github.com/o/r/pull/2170";
const [answered, unanswered] = pinned.asks;
let listed: Record<string, unknown> | null = null;
let readFails = false;
afterEach(() => { cleanup(); localStorage.clear(); listed = null; readFails = false; });

/** `AskAction` for run 2 at poll count 0, reading its asks from `asks` (a
 *  body, or a Response for a refusal) and posting to `post`, both with the
 *  stored bearer. */
async function show(asks: Record<string, unknown> | (() => Response), post = fakeFetch({ ok: true, detail: "asked" })) {
  storeToken(addressOf(base), "test-token");
  const read = fakeFetch(asks);
  const deps = { fetch: tokenedFetch(read.fetchImpl) };
  const action = (polls: number) => <AskAction base={base} runId={2} prUrl={prUrl} polls={polls} deps={deps} fetch={post.fetchImpl} />;
  const view = render(action(0));
  await act(settle);
  return { read: read.seen, post: post.seen, poll: async (polls: number) => { view.rerender(action(polls)); await act(settle); } };
}

/** `show` with no asks yet, or `listed` once set, failing while `readFails`,
 *  and the question box opened. */
async function openBox(post?: ReturnType<typeof fakeFetch>) {
  const shown = await show(() => readFails ? new Response("unavailable", { status: 503 })
    : Response.json(listed ?? { ...pinned, asks: [] }), post);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ask" })); });
  return shown;
}

test("an unanswered latest ask shows its question as waiting and offers no Ask", async () => {
  const { read } = await show(pinned);
  expect(read).toEqual([{ url: `${base}/runs/2/asks`, method: undefined, authorization: "Bearer test-token", body: undefined }]);
  expect(screen.getByText(`Asked: ${unanswered!.question}`)).toBeTruthy();
  expect(screen.getByText("waiting for the answer")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Ask" })).toBeNull();
});

test("an answered latest ask links its comment, else the row's pull request, shows the answer and offers Ask", async () => {
  await show({ ...pinned, asks: [answered] });
  const link = screen.getByRole("link", { name: "Answer on PR #2170" });
  expect(link.getAttribute("href")).toBe(answered!.url);
  expect(screen.getByText(answered!.answer!)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Ask" })).toBeTruthy();
  cleanup();
  await show({ ...pinned, asks: [{ ...answered, url: null }] });
  expect(screen.getByRole("link", { name: "Answer on PR #2170" }).getAttribute("href")).toBe(prUrl);
});

test("Ask opens an in-row question box; Send waits for text and Cancel posts nothing, with no browser dialog", async () => {
  const dialogs = [spyOn(window, "confirm"), spyOn(window, "alert"), spyOn(window, "prompt")];
  try {
    const { post } = await openBox();
    expect(post).toEqual([]);
    const group = screen.getByRole("group");
    const box = screen.getByRole("textbox", { name: "Question about the pull request" });
    expect(group.contains(box)).toBe(true);
    const sendButton = () => screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(sendButton().disabled).toBe(true);
    fireEvent.change(box, { target: { value: "   " } });
    expect(sendButton().disabled).toBe(true);
    fireEvent.change(box, { target: { value: "Why?" } });
    expect(sendButton().disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.getByRole("button", { name: "Ask" })).toBeTruthy();
    await act(settle);
    expect(post).toEqual([]);
    for (const dialog of dialogs) expect(dialog).not.toHaveBeenCalled();
  } finally { for (const dialog of dialogs) dialog.mockRestore(); }
});

/** `openBox` with `answer` as the daemon's reply, the question typed and Send clicked. */
async function sendWith(answer: Record<string, unknown>) {
  const shown = await openBox(fakeFetch(answer));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Why is the guest keyed by name?" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send" })); await settle(); });
  return shown;
}

test("Send posts the question once with the bearer, closes the box and shows it as waiting at once", async () => {
  const { post } = await sendWith({ ok: true, detail: "asked" });
  expect(post).toEqual([{ url: `${base}/actions/ask`, method: "POST", authorization: "Bearer test-token",
    body: { run: 2, question: "Why is the guest keyed by name?" } }]);
  expect(screen.queryByRole("group")).toBeNull();
  expect(screen.getByRole("status").textContent).toBe("asked");
  expect(screen.getByText("Asked: Why is the guest keyed by name?")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Ask" })).toBeNull();
});

test("a Send still in flight across a failed and recovered read cannot be posted again", async () => {
  let release!: () => void;
  const gate = new Promise<void>(done => { release = done; });
  const { post, poll } = await openBox(fakeFetch({ ok: true, detail: "asked" }, gate));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Why is the guest keyed by name?" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send" })); await settle(); });
  readFails = true;
  await poll(6);
  expect(screen.queryByRole("group")).toBeNull();
  readFails = false;
  await poll(12);
  const sendButton = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
  expect(sendButton.disabled).toBe(true);
  await act(async () => { fireEvent.click(sendButton); await settle(); });
  expect(post).toHaveLength(1);
  await act(async () => { release(); await settle(); });
  expect(post).toHaveLength(1);
  expect(screen.getByRole("status").textContent).toBe("asked");
  expect(screen.getByText("Asked: Why is the guest keyed by name?")).toBeTruthy();
});

test("a question sent after an earlier answer stays waiting until a read lists a new ask", async () => {
  listed = { ...pinned, asks: [answered] };
  const { read, poll } = await sendWith({ ok: true, detail: "asked" });
  const sent = "Asked: Why is the guest keyed by name?";
  expect(screen.getByText(sent)).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Answer on PR #2170" })).toBeNull();
  await poll(6);
  expect(read).toHaveLength(2);
  expect(screen.getByText(sent)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Ask" })).toBeNull();
  const later = { ...answered!, id: 11, url: `${prUrl}#issuecomment-950`, answer: "The schema keys guests by name." };
  listed = { ...pinned, asks: [answered, later] };
  await poll(12);
  expect(screen.queryByText(sent)).toBeNull();
  expect(screen.getByRole("link", { name: "Answer on PR #2170" }).getAttribute("href")).toBe(later.url);
  expect(screen.getByText(later.answer)).toBeTruthy();
});

test("a refused ask shows the daemon's detail and keeps the question in the box", async () => {
  await sendWith({ ok: false, reason: "ask_pending", detail: "an earlier ask is waiting" });
  expect(screen.getByRole("status").textContent).toBe("an earlier ask is waiting");
  expect((screen.getByRole("textbox", { name: "Question about the pull request" }) as HTMLTextAreaElement).value)
    .toBe("Why is the guest keyed by name?");
});

test("a daemon without the asks route offers no Ask", async () => {
  const { read } = await show(() => new Response("not found", { status: 404 }));
  expect(read).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Ask" })).toBeNull();
});
