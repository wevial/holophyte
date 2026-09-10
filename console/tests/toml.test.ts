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

test("reading the bound keys: a string with its comment, an integer, a boolean; a missing key and a missing table are undefined", () => {
  expect(readKey(TEXT, { table: "agents", key: "implementer" })).toBe("claude --model opus -p");
  expect(readKey(TEXT, { table: "loop", key: "workers" })).toBe(1);
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

test("adding a setup line rewrites a one-line array on its line, the trailing comment kept and no other byte touched", () => {
  const text = `[worktree]\n# what each worktree runs first\nsetup = ["make deps"]  # keep short\n\n[merge]\nafter = []\n`;
  const added = writeKey(text, { table: "worktree", key: "setup" }, ["make deps", "bun install"]);
  expect(added).toBe(`[worktree]\n# what each worktree runs first\nsetup = ["make deps", "bun install"] # keep short\n\n[merge]\nafter = []\n`);
  expect(readKey(added, { table: "worktree", key: "setup" })).toEqual(["make deps", "bun install"]);
  const after = writeKey(text, { table: "merge", key: "after" }, ["git push"]);
  expect(readKey(after, { table: "merge", key: "after" })).toEqual(["git push"]);
  expect(after).toContain('after = ["git push"]\n');
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

test("a key in a multi-line array is found but unbound: its lines are its source, its value undefined, and the one-line keys around it still bind", () => {
  // TEXT's `[worktree] setup` spans four lines with an inner comment.
  const setup = findKey(TEXT, { table: "worktree", key: "setup" });
  expect(setup).toMatchObject({ value: undefined, raw: '[\n  "make deps",   # first\n  "bun install",\n]' });
  expect(setup!.end - setup!.start).toBe(4);
  // Items sharing the bracket lines are still a multi-line array.
  const shared = `[worktree]\nsetup = ["a", # keep this\n  "b"]\n`;
  expect(readKey(shared, { table: "worktree", key: "setup" })).toBeUndefined();
  expect(findKey(shared, { table: "worktree", key: "setup" })?.raw).toBe('["a", # keep this\n  "b"]');
  // The multi-line value never hides the plain keys after it.
  expect(readKey(TEXT, { table: "linear", key: "api_key" })).toBe("[redacted]");
});

test("a key under a quoted or dotted table header is found but unbound, so the sheet neither draws it empty nor appends a duplicate table", () => {
  const quoted = `["loop"]\nworkers = 1  # one\n`;
  expect(readKey(quoted, { table: "loop", key: "workers" })).toBeUndefined();
  expect(findKey(quoted, { table: "loop", key: "workers" })).toMatchObject({ start: 1, end: 2, value: undefined });
  const dotted = `[ 'a' . "b" ]\nx = true\n`;
  expect(findKey(dotted, { table: "a.b", key: "x" })).toMatchObject({ raw: "true", value: undefined });
  // The same line under the plain header binds.
  expect(readKey(`[loop]\nworkers = 1  # one\n`, { table: "loop", key: "workers" })).toBe(1);
});

test("a quoted table name holding `]` closes the table before it, so the key under it is never bound to `[loop]` nor overwritten by its edit", () => {
  const text = `[loop]\n["other]table"]\nworkers = 9\n`;
  expect(findKey(text, { table: "loop", key: "workers" })).toBeNull();
  expect(findKey(text, { table: "other]table", key: "workers" })).toMatchObject({ start: 2, end: 3, raw: "9", value: undefined });
  expect(writeKey(text, { table: "loop", key: "workers" }, 3)).toBe(`[loop]\nworkers = 3\n["other]table"]\nworkers = 9\n`);
  // A padded plain header still binds.
  expect(readKey(`[ loop ]\nworkers = 1\n`, { table: "loop", key: "workers" })).toBe(1);
});

test("a key held in a shape the editor does not bind is found but unread: a triple-quoted string, an inline table and a float each keep their source and read as undefined", () => {
  const text = `[agents]
implementer = """
claude --model opus -p
"""
review_model = { name = "opus" }

[loop]
workers = 1.0
`;
  const implementer = findKey(text, { table: "agents", key: "implementer" });
  expect(implementer?.value).toBeUndefined();
  expect(implementer?.raw).toBe('"""\nclaude --model opus -p\n"""');
  expect(findKey(text, { table: "agents", key: "review_model" })).toMatchObject({ value: undefined, raw: '{ name = "opus" }' });
  expect(findKey(text, { table: "loop", key: "workers" })).toMatchObject({ value: undefined, raw: "1.0" });
  // A key the table lacks is null, not unbound, so the sheet draws it empty and editable.
  expect(findKey(text, { table: "agents", key: "review_effort" })).toBeNull();
});

test("a table the root defines without a header, inline or by dotted keys, or a dotted key in a body, is found but unbound and never rewritten", () => {
  const inline = `loop = { workers = 1 }\n\n[agents]\nimplementer = "claude -p"\n`;
  expect(findKey(inline, { table: "loop", key: "workers" })).toMatchObject({ start: 0, end: 1, raw: "loop = { workers = 1 }", value: undefined });
  const dotted = `# root\nloop.workers = 1  # one\n[agents]\nimplementer = "claude -p"\n`;
  expect(findKey(dotted, { table: "loop", key: "workers" })).toMatchObject({ start: 1, end: 2, raw: "loop.workers = 1", value: undefined });
  // The table exists in that shape even when the key does not, so a `[loop]`
  // header cannot be appended: the field is unbound, not empty.
  const sibling = `loop.sweep_interval_sec = 5\n`;
  expect(findKey(sibling, { table: "loop", key: "workers" })).toMatchObject({ value: undefined });
  // A dotted key in the body makes `workers` a table, not a value.
  const body = `[loop]\nworkers.max = 3\n`;
  expect(findKey(body, { table: "loop", key: "workers" })).toMatchObject({ raw: "workers.max = 3", value: undefined });
  // None of these are edited: the text comes back byte for byte.
  for (const text of [inline, dotted, sibling, body]) {
    expect(writeKey(text, { table: "loop", key: "workers" }, 3)).toBe(text);
    expect(deleteKey(text, { table: "loop", key: "workers" })).toBe(text);
  }
  // A root key of another table leaves `[loop] workers` as it was.
  expect(readKey(`serve.port = 8\n[loop]\nworkers = 2\n`, { table: "loop", key: "workers" })).toBe(2);
  expect(findKey(`serve.port = 8\n`, { table: "loop", key: "workers" })).toBeNull();
});
