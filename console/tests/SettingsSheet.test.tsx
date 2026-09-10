import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Floor } from "../src/components/Floor";
import { CONFIG_EDIT_OFF } from "../src/components/SettingsSheet";
import type { Fetch } from "../src/lib/poll";
import type { Run, Status } from "../src/lib/types";
import { fixture, settle } from "./harness";

const working = await fixture<Status>("working.json");
const RUN: Run = {
  id: 52,
  ticket: "KO-219",
  title: "The sweep frees a silent lease",
  phase: "working",
  round: 0,
  strikes: 0,
  started_ms: working.now - 72_000,
  heartbeat_age_ms: 71_000,
  elapsed_ms: 72_000,
  time_box_ms: 1_500_000,
  host: "writer",
};
const BASE = "http://writer:7710";
const editable: Status = { ...working, runs: [RUN], actions: true, config_edit: true };

const TEXT = `[serve]
token_file = "/home/op/serve.token"
config_edit = true

[agents]
implementer = "claude --model opus -p"   # the default route

[loop]
workers = 1   # one at a time

[worktree]
setup = ["make deps"]
`;

afterEach(cleanup);

/** A daemon serving `/config` from `text` and answering a `PUT` with
 *  `verdict()`; every `PUT` body is kept for the test to read. */
function daemon(text: string, verdict: () => Response = () => Response.json({ ok: true, path: "/srv/x/config.toml", backup: "/srv/x/config.toml.bak-1", applies: "next loop start" })) {
  const puts: { text: string; auth: string | null }[] = [];
  const fetch: Fetch = async (url, init) => {
    if (!url.endsWith("/config")) return new Response("not found", { status: 404 });
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as { text: string };
      puts.push({ text: body.text, auth: new Headers(init.headers).get("authorization") });
      return verdict();
    }
    return Response.json({ text, path: "/srv/x/config.toml", applies: "next loop start" });
  };
  return { fetch, puts };
}

const noop = () => {};
const field = (id: string) => screen.getByRole("dialog").querySelector(`[data-field="${id}"]`) as HTMLInputElement;

async function open(status: Status, fetch: Fetch) {
  render(<Floor daemons={[{ base: BASE, status }]} project="all" expandedRun={null} onToggleRun={noop} deps={{ fetch }} />);
  const entry = within(screen.getByRole("region", { name: "writer" })).getByRole("button", { name: "Settings" });
  entry.focus();
  fireEvent.click(entry);
  await act(settle);
  return entry;
}

test("the block's Settings entry opens a dialog whose fields show [agents] implementer and [loop] workers from GET /config; the raw tab holds the text", async () => {
  const { fetch } = daemon(TEXT);
  expect(screen.queryByRole("dialog")).toBeNull();
  const entry = await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(document.activeElement).toBe(dialog);
  expect(within(dialog).getByRole("heading", { level: 2 }).textContent).toBe("writer");
  expect(field("agents.implementer").value).toBe("claude --model opus -p");
  expect(field("loop.workers").value).toBe("1");
  expect(field("worktree.setup").value).toBe("make deps");
  expect(field("agents.review_effort").value).toBe("");
  expect(dialog.querySelector("[data-config-edit-off]")).toBeNull();

  fireEvent.click(within(dialog).getByRole("tab", { name: "Raw TOML" }));
  const raw = within(dialog).getByRole("textbox", { name: "Raw TOML" }) as HTMLTextAreaElement;
  expect(raw.value).toBe(TEXT);
  // An edit in the raw tab is what the fields re-read.
  fireEvent.change(raw, { target: { value: TEXT.replace("workers = 1", "workers = 4") } });
  fireEvent.click(within(dialog).getByRole("tab", { name: "Fields" }));
  expect(field("loop.workers").value).toBe("4");

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(entry);
});

test("workers changed to 3 and Save clicked: the PUT carries the text differing from the original on that line alone, and the verdict shows", async () => {
  const { fetch, puts } = daemon(TEXT);
  await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  const save = within(dialog).getByRole("button", { name: "Save" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  fireEvent.change(field("loop.workers"), { target: { value: "3" } });
  expect(save.disabled).toBe(false);
  fireEvent.click(save);
  await act(settle);

  expect(puts.length).toBe(1);
  const before = TEXT.split("\n");
  const after = puts[0]!.text.split("\n");
  expect(after.length).toBe(before.length);
  const changed = before.map((line, at) => (line === after[at] ? null : at)).filter((at): at is number => at != null);
  expect(changed).toEqual([8]);
  expect(after[8]).toBe("workers = 3 # one at a time");
  expect(dialog.querySelector("[data-save-verdict]")!.textContent).toContain("next loop start");
  expect(dialog.querySelector("[data-save-verdict]")!.textContent).toContain("config.toml.bak-1");
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(dialog.querySelector("[data-applies]")!.textContent).toContain("next start");
  expect((within(dialog).getByRole("button", { name: "Restart supervisor" }) as HTMLButtonElement).disabled).toBe(false);
});

test("a 400 naming [loop] workers shows the daemon's sentence under the workers field and the sheet stays open; one naming no field lands under the raw tab", async () => {
  let error = "[holo2] /srv/x/config.toml: [loop] workers must be an integer >= 1, got 0";
  const { fetch, puts } = daemon(TEXT, () => Response.json({ ok: false, error }, { status: 400 }));
  await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  fireEvent.change(field("loop.workers"), { target: { value: "0" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);

  expect(puts.length).toBe(1);
  expect(screen.getByRole("dialog")).toBe(dialog);
  const under = dialog.querySelector('[data-field-error="loop.workers"]')!;
  expect(under.textContent).toBe(error);
  expect(field("loop.workers").getAttribute("aria-describedby")).toBe(under.id);
  expect(dialog.querySelector("[data-raw-error]")).toBeNull();
  expect(dialog.querySelector("[data-save-verdict]")).toBeNull();

  error = "malformed TOML: Expected '=' after a key (at line 3, column 1)";
  fireEvent.change(field("loop.workers"), { target: { value: "2" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);
  expect(puts.length).toBe(2);
  expect(within(dialog).getByRole("tab", { name: "Raw TOML" }).getAttribute("aria-selected")).toBe("true");
  expect(dialog.querySelector("[data-raw-error]")!.textContent).toBe(error);
  expect(dialog.querySelector("[data-field-error]")).toBeNull();
});

test("a daemon whose /status lacks config_edit opens the sheet read-only, every field inert and the enabling key named", async () => {
  const { fetch, puts } = daemon(TEXT);
  const { config_edit: _off, ...withoutFlag } = editable;
  await open(withoutFlag, fetch);
  const dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("[data-config-edit-off]")!.textContent).toBe(CONFIG_EDIT_OFF);
  expect(CONFIG_EDIT_OFF).toContain("[serve] config_edit");
  const controls = Array.from(dialog.querySelectorAll("[data-field]")) as (HTMLInputElement | HTMLSelectElement)[];
  expect(controls.length).toBe(9);
  for (const control of controls) {
    expect(control instanceof HTMLSelectElement ? control.disabled : control.readOnly).toBe(true);
  }
  expect(field("agents.implementer").value).toBe("claude --model opus -p");
  const save = within(dialog).getByRole("button", { name: "Save" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  expect(save.title).toBe(CONFIG_EDIT_OFF);
  fireEvent.click(save);
  await act(settle);
  expect(puts.length).toBe(0);
  fireEvent.click(within(dialog).getByRole("tab", { name: "Raw TOML" }));
  expect((within(dialog).getByRole("textbox", { name: "Raw TOML" }) as HTMLTextAreaElement).readOnly).toBe(true);
});
