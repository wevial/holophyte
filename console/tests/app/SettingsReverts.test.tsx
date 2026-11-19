import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { ConfigBody, ConfigValues } from "../../src/lib/config";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, NO_ATTENTION, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const R1 = "3f9a2c1" + "a".repeat(57);
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;

/** Settings, General of a daemon whose config.toml reads `values`; each
 *  `PUT /config` body is kept. */
async function mount(values: ConfigValues) {
  const config: ConfigBody = {
    text: "", values, path: "/srv/demo/config.toml", applies: "next loop start", revision: R1,
    saved: { at_ms: 0, actor: "maintainer" }, modified_ms: 0,
  };
  const status: Status = { ...working, project: "/srv/demo", host: "writer-host", runs: [], config_edit: true, actions: true };
  const poll = peersFetch(ORIGIN, { [ORIGIN]: { status, attention: { ...NO_ATTENTION, project: status.project } } });
  const writes: Record<string, unknown>[] = [];
  const fetch: Fetch = async (url, init) => {
    if (!url.endsWith("/config")) return poll(url, init);
    if (init?.method !== "PUT") return Response.json(config);
    writes.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    return Response.json({ revision: R1, applies: "next loop start", backup: "/srv/demo/config.toml.bak" });
  };
  window.location.hash = "#/settings/general?project=demo";
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={ORIGIN} pollDeps={fakeDeps(fetch).deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return writes;
}

const row = (key: string) => document.querySelector<HTMLElement>(`[data-setting="${key}"]`)!;
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("General shows a Reverts switch, off with no [revert] table, and a Never revert list of the daemon's default globs", async () => {
  await mount({ loop: { workers: 2 } });
  const reverts = within(row("revert.enabled")).getByRole("switch", { name: "Reverts" });
  expect(reverts.getAttribute("aria-checked")).toBe("false");
  const never = within(row("revert.refuse_paths")).getByRole("textbox", { name: "Never revert" }) as HTMLTextAreaElement;
  expect(never.value).toBe("**/migrations/**\n**/migrate/**\n**/*.sql\n**/schema.prisma");
});

test("saving a turned-on Reverts switch and an edited Never revert list writes revert.enabled and revert.refuse_paths", async () => {
  const writes = await mount({ loop: { workers: 2 } });
  fireEvent.click(screen.getByRole("switch", { name: "Reverts" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Never revert" }), { target: { value: "store/schema.py\n\n**/*.sql" } });
  fireEvent.click(within(screen.getByRole("region", { name: "Unsaved changes" })).getByRole("button", { name: "Save" }));
  const dialog = screen.getByRole("dialog", { name: "Save these changes?" });
  expect(dialog.textContent).toContain("[revert]");
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  await act(settle);
  await act(settle);
  expect(writes).toHaveLength(1);
  expect(writes[0]!.patch).toEqual({ "revert.enabled": true, "revert.refuse_paths": ["store/schema.py", "**/*.sql"] });
});
