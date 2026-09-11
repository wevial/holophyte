import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Floor } from "../src/components/Floor";
import { CONFIG_EDIT_OFF, UNBOUND_NOTE } from "../src/components/SettingsSheet";
import type { ConfigValues } from "../src/lib/config";
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
const VALUES: ConfigValues = {
  serve: { token_file: "/home/op/serve.token", config_edit: true },
  agents: { implementer: "claude --model opus -p" },
  loop: { workers: 1 },
  worktree: { setup: ["make deps"] },
};

afterEach(cleanup);

const ACCEPTED = () => Response.json({ ok: true, path: "/srv/x/config.toml", backup: "/srv/x/config.toml.bak-1", applies: "next loop start" });

/** A daemon serving `/config` as `text` and `values` and answering a
 *  `PUT` with `verdict()`; every `PUT` body is kept, parsed, for the test
 *  to read, and every `GET` counted. */
function daemon(text: string, values: ConfigValues | null, verdict: () => Response = ACCEPTED) {
  const puts: Record<string, unknown>[] = [];
  const gets = { count: 0 };
  const fetch: Fetch = async (url, init) => {
    if (!url.endsWith("/config")) return new Response("not found", { status: 404 });
    if (init?.method === "PUT") {
      puts.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return verdict();
    }
    gets.count += 1;
    return Response.json({ text, values, path: "/srv/x/config.toml", applies: "next loop start" });
  };
  return { fetch, puts, gets };
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

test("the block's Settings entry opens a dialog whose fields show [agents] implementer and [loop] workers from GET /config's values; the raw tab holds the text", async () => {
  const { fetch } = daemon(TEXT, VALUES);
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

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(entry);
});

test("workers changed to 3 and Save clicked: the PUT body is a patch of that one dotted key and nothing else, and the verdict shows", async () => {
  const { fetch, puts, gets } = daemon(TEXT, VALUES);
  await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  const save = within(dialog).getByRole("button", { name: "Save" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  // A field put back to the file's own value is not a change.
  fireEvent.change(field("agents.implementer"), { target: { value: "codex" } });
  fireEvent.change(field("agents.implementer"), { target: { value: "claude --model opus -p" } });
  fireEvent.change(field("loop.workers"), { target: { value: "3" } });
  expect(save.disabled).toBe(false);
  expect(gets.count).toBe(1);
  fireEvent.click(save);
  await act(settle);

  expect(puts).toEqual([{ patch: { "loop.workers": 3 } }]);
  expect(gets.count).toBe(2);
  expect(dialog.querySelector("[data-save-verdict]")!.textContent).toContain("next loop start");
  expect(dialog.querySelector("[data-save-verdict]")!.textContent).toContain("config.toml.bak-1");
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(dialog.querySelector("[data-applies]")!.textContent).toContain("next start");
  expect((within(dialog).getByRole("button", { name: "Restart supervisor" }) as HTMLButtonElement).disabled).toBe(false);
});

test("the raw tab edited and Save clicked: the PUT body carries text and no patch, even after a field edit", async () => {
  const { fetch, puts } = daemon(TEXT, VALUES);
  await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  fireEvent.change(field("loop.workers"), { target: { value: "3" } });
  fireEvent.click(within(dialog).getByRole("tab", { name: "Raw TOML" }));
  const edited = TEXT.replace("workers = 1", "workers = 4");
  fireEvent.change(dialog.querySelector("[data-raw]")!, { target: { value: edited } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);

  expect(puts.length).toBe(1);
  expect(puts[0]).toEqual({ text: edited });
  expect(puts[0]).not.toHaveProperty("patch");
});

test("a 400 naming [loop] workers shows the daemon's sentence under the workers field and the sheet stays open; one naming no field lands under the raw tab", async () => {
  let error = "[holo2] /srv/x/config.toml: [loop] workers must be an integer of at least 1, got 0";
  const { fetch, puts } = daemon(TEXT, VALUES, () => Response.json({ ok: false, error }, { status: 400 }));
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
  expect(field("loop.workers").value).toBe("0");
  expect(dialog.querySelector("[data-raw-error]")).toBeNull();
  expect(dialog.querySelector("[data-save-verdict]")).toBeNull();

  error = "malformed TOML on disk: Expected '=' after a key (at line 3, column 1)";
  fireEvent.change(field("loop.workers"), { target: { value: "2" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);
  expect(puts.length).toBe(2);
  expect(within(dialog).getByRole("tab", { name: "Raw TOML" }).getAttribute("aria-selected")).toBe("true");
  expect(dialog.querySelector("[data-raw-error]")!.textContent).toBe(error);
  expect(dialog.querySelector("[data-field-error]")).toBeNull();
});

test("a refusal naming a dotted patch key, or [loop] workers after a save from the raw tab, selects the Fields tab so the sentence is on screen", async () => {
  let error = "loop.workers: a patch value is a string, an integer, a boolean or a list of strings, not float";
  const { fetch, puts } = daemon(TEXT, VALUES, () => Response.json({ ok: false, error }, { status: 400 }));
  await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  fireEvent.change(field("loop.workers"), { target: { value: "5" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);
  expect(puts.length).toBe(1);
  expect(dialog.querySelector('[data-field-error="loop.workers"]')!.textContent).toBe(error);

  error = "[holo2] /srv/x/config.toml: [loop] workers must be an integer of at least 1, got 0";
  fireEvent.click(within(dialog).getByRole("tab", { name: "Raw TOML" }));
  fireEvent.change(dialog.querySelector("[data-raw]")!, { target: { value: TEXT.replace("workers = 1", "workers = 0") } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);

  expect(puts.length).toBe(2);
  expect(puts[1]).toHaveProperty("text");
  expect(within(dialog).getByRole("tab", { name: "Fields" }).getAttribute("aria-selected")).toBe("true");
  expect(within(dialog).getByRole("alert").textContent).toBe(error);
  expect(dialog.querySelector('[data-field-error="loop.workers"]')!.textContent).toBe(error);
});

test("a daemon whose /status lacks config_edit opens the sheet read-only, every field inert and the enabling key named", async () => {
  const { fetch, puts } = daemon(TEXT, VALUES);
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

test("a key the daemon parsed in another shape than the field edits opens read-only under the raw-tab note, while the keys beside it stay editable", async () => {
  const values: ConfigValues = { ...VALUES, agents: { implementer: ["claude", "-p"] } };
  const { fetch, puts } = daemon(TEXT, values);
  await open(editable, fetch);
  const dialog = screen.getByRole("dialog");
  const implementer = field("agents.implementer");
  expect(implementer.readOnly).toBe(true);
  expect(implementer.value).toBe('["claude","-p"]');
  expect(dialog.querySelector('[data-unbound-note="agents.implementer"]')?.textContent).toBe(UNBOUND_NOTE);
  fireEvent.change(implementer, { target: { value: "codex" } });
  expect(field("agents.implementer").value).toBe('["claude","-p"]');
  expect(field("loop.workers").readOnly).toBe(false);
  fireEvent.change(field("loop.workers"), { target: { value: "3" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);
  expect(puts).toEqual([{ patch: { "loop.workers": 3 } }]);
});
