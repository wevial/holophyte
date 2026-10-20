import { expect, test } from "bun:test";
import { tomlTokenLines, type TomlKind } from "../../src/next/tomlTokens";

test("each line's spans carry the TOML kinds in order and concatenate back to the line", () => {
  const cases: [string, TomlKind[]][] = [
    ["[merge]", ["table"]],
    ["[[agents.routes]]", ["table"]],
    ['mode = "pr"  # GitHub', ["key", "punct", "string", "comment"]],
    ["retention_days = 30", ["key", "punct", "number"]],
    ["pr_draft = true", ["key", "punct", "bool"]],
    ["since = 2026-10-20T08:00:00Z", ["key", "punct", "date"]],
    ["name = 'literal # not a comment'", ["key", "punct", "string"]],
  ];
  for (const [line, kinds] of cases) {
    const spans = tomlTokenLines(line)[0]!;
    expect({ line, kinds: spans.map(span => span.kind) }).toEqual({ line, kinds });
    expect(spans.map(span => span.text).join("")).toBe(line);
  }
});

test("a multi-line basic string keeps its middle line a string, never a comment or key", () => {
  const text = 'note = """\nfirst # not a comment = x\nlast"""';
  const lines = tomlTokenLines(text);
  expect(lines.map(spans => spans.map(span => span.text).join(""))).toEqual(text.split("\n"));
  expect(lines[0]!.at(-1)).toEqual({ kind: "string", text: '"""' });
  expect(lines[1]).toEqual([{ kind: "string", text: "first # not a comment = x" }]);
  expect(lines[2]).toEqual([{ kind: "string", text: 'last"""' }]);
});
