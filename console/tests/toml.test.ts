import { expect, test } from "bun:test";
import { deleteKey, findKey, namedKey, readKey, writeKey } from "../src/lib/toml";

/** A config the way an operator writes one: comments, blank lines,
 *  a table the sheet does not bind, an array-of-tables whose name would
 *  shadow a plain table, and a secret the daemon has redacted. */
const TEXT = `# The writer's config
[serve]
token_file = "/home/op/serve.token"  # kept as a path
config_edit = true

[agents]
implementer = "claude --model opus -p"   # default route
review_effort = "medium"

[loop]
# up to three at once
workers = 1                  # 3: up to three tickets worked at once
sweep_interval_sec = 120

[worktree]
setup = [
  "make deps",   # first
  "bun install",
]

[linear]
api_key = "[redacted]"

[[hooks]]
workers = 9
token = "[redacted]"
`;

test("reading the bound keys: a string with its comment, an integer, a multi-line array; a missing key and a missing table are undefined", () => {
  expect(readKey(TEXT, { table: "agents", key: "implementer" })).toBe("claude --model opus -p");
  expect(readKey(TEXT, { table: "loop", key: "workers" })).toBe(1);
  expect(readKey(TEXT, { table: "worktree", key: "setup" })).toEqual(["make deps", "bun install"]);
  expect(readKey(TEXT, { table: "serve", key: "config_edit" })).toBe(true);
  expect(readKey(TEXT, { table: "loop", key: "review_rounds" })).toBeUndefined();
  expect(readKey(TEXT, { table: "merge", key: "mode" })).toBeUndefined();
  // `[[hooks]] workers` is not `[hooks] workers`, and never `[loop] workers`.
  expect(readKey(TEXT, { table: "hooks", key: "workers" })).toBeUndefined();
});

test("setting workers to 3 changes exactly that line, its trailing comment and every other byte intact", () => {
  const edited = writeKey(TEXT, { table: "loop", key: "workers" }, 3);
  const before = TEXT.split("\n");
  const after = edited.split("\n");
  expect(after.length).toBe(before.length);
  const changed = before.map((line, at) => (line === after[at] ? null : at)).filter((at): at is number => at != null);
  expect(changed.length).toBe(1);
  expect(before[changed[0]!]).toBe("workers = 1                  # 3: up to three tickets worked at once");
  expect(after[changed[0]!]).toBe("workers = 3 # 3: up to three tickets worked at once");
  expect(readKey(edited, { table: "loop", key: "workers" })).toBe(3);
  // The array-of-tables entry with the same key name is untouched.
  expect(edited).toContain("[[hooks]]\nworkers = 9\n");
});

test("a string value is written escaped and read back; a multi-line array collapses to one line in place", () => {
  const command = 'codex --flag "quoted" \\ backslash';
  const edited = writeKey(TEXT, { table: "agents", key: "implementer" }, command);
  expect(readKey(edited, { table: "agents", key: "implementer" })).toBe(command);
  expect(edited.split("\n").length).toBe(TEXT.split("\n").length);

  const setup = writeKey(TEXT, { table: "worktree", key: "setup" }, ["make deps", "bun install", "go build ./..."]);
  const hit = findKey(setup, { table: "worktree", key: "setup" })!;
  expect(hit.end - hit.start).toBe(1);
  expect(setup.split("\n")[hit.start]).toBe('setup = ["make deps", "bun install", "go build ./..."]');
  expect(setup.split("\n").length).toBe(TEXT.split("\n").length - 3);
  expect(setup).toContain("\n[linear]\napi_key = \"[redacted]\"\n");
});

test("a key the table lacks lands after its last key; a table the text lacks is appended; deleting drops only the key's line", () => {
  const withModel = writeKey(TEXT, { table: "agents", key: "review_model" }, "gpt-5.6-sol");
  expect(withModel).toContain('review_effort = "medium"\nreview_model = "gpt-5.6-sol"\n\n[loop]');
  expect(withModel.split("\n").length).toBe(TEXT.split("\n").length + 1);

  const withMerge = writeKey(TEXT, { table: "merge", key: "mode" }, "pr");
  expect(withMerge.startsWith(TEXT.trimEnd())).toBe(true);
  expect(withMerge.endsWith('\n\n[merge]\nmode = "pr"\n')).toBe(true);
  expect(readKey(withMerge, { table: "merge", key: "mode" })).toBe("pr");
  expect(writeKey("", { table: "merge", key: "mode" }, "pr")).toBe('[merge]\nmode = "pr"\n');

  const dropped = deleteKey(TEXT, { table: "loop", key: "workers" });
  expect(dropped.split("\n").length).toBe(TEXT.split("\n").length - 1);
  expect(readKey(dropped, { table: "loop", key: "workers" })).toBeUndefined();
  expect(dropped).toContain("# up to three at once\nsweep_interval_sec = 120\n");
  expect(deleteKey(TEXT, { table: "loop", key: "absent" })).toBe(TEXT);
});

test("the daemon's refusal names its key the loader's way; a sentence without one is null", () => {
  expect(namedKey("[holo2] /srv/x/config.toml: [loop] workers must be an integer >= 1, got 0")).toEqual({ table: "loop", key: "workers" });
  expect(namedKey("[holo2] /srv/x/config.toml: [agents] review_effort must be one of low, medium")).toEqual({
    table: "agents",
    key: "review_effort",
  });
  expect(namedKey("malformed TOML: Expected '=' after a key (at line 3, column 1)")).toBeNull();
});
