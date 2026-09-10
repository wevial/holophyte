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

test("a string value is written escaped and read back; an unreadable escape is unbound rather than a throw", () => {
  const command = 'codex --flag "quoted" \\ backslash';
  const edited = writeKey(TEXT, { table: "agents", key: "implementer" }, command);
  expect(readKey(edited, { table: "agents", key: "implementer" })).toBe(command);
  expect(edited.split("\n").length).toBe(TEXT.split("\n").length);
  // `\UFFFFFFFF` is outside Unicode: `String.fromCodePoint` would throw a
  // RangeError, which the sheet's render must never see.
  expect(readKey('[agents]\nimplementer = "\\UFFFFFFFF"\n', { table: "agents", key: "implementer" })).toBeUndefined();
  expect(readKey('[agents]\nimplementer = "\\uD83D"\n', { table: "agents", key: "implementer" })).toBeUndefined();
  expect(readKey('[agents]\nimplementer = "\\U0001F600"\n', { table: "agents", key: "implementer" })).toBe("\u{1F600}");
});

test("adding a setup line edits the multi-line array in place: its inner and trailing comments stay, the new item lands before the bracket", () => {
  const setup = writeKey(TEXT, { table: "worktree", key: "setup" }, ["make deps", "bun install", "go build ./..."]);
  const before = TEXT.split("\n");
  const after = setup.split("\n");
  expect(after.length).toBe(before.length + 1);
  expect(readKey(setup, { table: "worktree", key: "setup" })).toEqual(["make deps", "bun install", "go build ./..."]);
  const bracket = before.indexOf("]");
  expect(after.slice(0, bracket)).toEqual(before.slice(0, bracket));
  expect(after[bracket]).toBe('  "go build ./...",');
  expect(after.slice(bracket + 1)).toEqual(before.slice(bracket));
  expect(setup).toContain('  "make deps",   # first\n');

  // Dropping the first item takes its line and its comment with it; an
  // item edited in place keeps its line's tail; the array's own comment
  // lines survive either way.
  const commented = TEXT.replace('setup = [\n', 'setup = [\n  # retain this explanation\n');
  const dropped = writeKey(commented, { table: "worktree", key: "setup" }, ["bun install"]);
  expect(dropped).toContain('setup = [\n  # retain this explanation\n  "bun install",\n]\n');
  expect(dropped).not.toContain("make deps");
  const replaced = writeKey(commented, { table: "worktree", key: "setup" }, ["make dep", "bun install"]);
  expect(replaced).toContain('setup = [\n  # retain this explanation\n  "make dep",   # first\n  "bun install",\n]\n');

  // A one-line array stays one line.
  const oneLine = writeKey("[worktree]\nsetup = [\"a\"]  # x\n", { table: "worktree", key: "setup" }, ["a", "b"]);
  expect(oneLine).toBe('[worktree]\nsetup = ["a", "b"] # x\n');
});

test("a header or a key inside a multi-line string is neither: the real [loop] workers is read and edited, the implementer text untouched", () => {
  const text = `[agents]
implementer = """
[loop]
workers = 7
"""

[loop]
workers = 1
`;
  expect(readKey(text, { table: "loop", key: "workers" })).toBe(1);
  expect(readKey(text, { table: "agents", key: "implementer" })).toBeUndefined();
  const edited = writeKey(text, { table: "loop", key: "workers" }, 3);
  expect(edited).toBe(text.replace("\nworkers = 1\n", "\nworkers = 3\n"));
  expect(edited).toContain('"""\n[loop]\nworkers = 7\n"""');
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
