import { expect, test } from "bun:test";
import { HeaderDate, readHeader, withKey, type Table } from "../../src/app/ticketHeader";

const NOTE_LINE = String.raw`note = "the \"Open preview\" link \\ kept"`;
const SOURCE = `ui_change = "major" # the preview pane
estimate = 45
depends_on = [
  "HOLO-1", "HOLO-2",
]

[mockup]
url = 'https://claude.ai/artifact/abc123'
approved = 2026-10-19
${NOTE_LINE}

[[mockup.boards]]
url = "https://media.example/row.png"
caption = "Row, Dawn"

[[evidence]]
file = "01-row.png"
state = "the row at dawn"

[[evidence]]
file = "02-flow.webm"
state = "opening the preview"
`;

test("the reader gives escaped and literal strings, a multi-line array with a trailing comma, a date, board captions and evidence entries in order", () => {
  expect(readHeader(SOURCE)).toEqual({
    ui_change: "major",
    estimate: 45,
    depends_on: ["HOLO-1", "HOLO-2"],
    mockup: {
      url: "https://claude.ai/artifact/abc123",
      approved: new HeaderDate("2026-10-19"),
      note: 'the "Open preview" link \\ kept',
      boards: [{ url: "https://media.example/row.png", caption: "Row, Dawn" }],
    },
    evidence: [
      { file: "01-row.png", state: "the row at dawn" },
      { file: "02-flow.webm", state: "opening the preview" },
    ],
  });
});

test("a note read back is written as the same line, after approved when the table lacks it", () => {
  const note = (readHeader(SOURCE).mockup as Table).note as string;
  expect(withKey(SOURCE.replace(`${NOTE_LINE}\n`, ""), "mockup", "note", note)).toBe(SOURCE);
});

test("rewriting a key keeps the comment at the end of its line and every other line", () => {
  expect(withKey(SOURCE, "", "ui_change", "minor")).toBe(SOURCE.replace('ui_change = "major" # the preview pane', 'ui_change = "minor" # the preview pane'));
});

test("a [__proto__] table is read as the header's own table and leaves other headers without its keys", () => {
  const header = readHeader("[__proto__]\nestimate = 65\n");
  expect(Object.keys(header)).toEqual(["__proto__"]);
  expect(readHeader('ui_change = "none"\n').estimate).toBeUndefined();
  expect(({} as Record<string, unknown>).estimate).toBeUndefined();
});

test("setting a key whose value the reader cannot read replaces its line, keeping its comment, instead of adding a second key", () => {
  const source = 'ui_change = "none"\nestimate = 25.0 # rounded\ndepends_on = []\n';
  expect(readHeader(source).estimate).toBeUndefined();
  expect(withKey(source, "", "estimate", 30)).toBe('ui_change = "none"\nestimate = 30 # rounded\ndepends_on = []\n');
  expect(withKey('note = "a # b\', c\n', "", "note", "d")).toBe('note = "d"\n');
  const trailing = 'estimate = 45 min\n';
  expect(readHeader(trailing).estimate).toBeUndefined();
  expect(withKey(trailing, "", "estimate", 45)).toBe("estimate = 45\n");
});

test("editing a multi-line array keeps its comment rows and the rows of items that stay", () => {
  const source = 'depends_on = [ # blockers\n  "HOLO-1", # the store\n  # HOLO-3 lands after review\n  "HOLO-2" # the board\n]\nestimate = 30\n';
  expect(withKey(source, "", "depends_on", ["HOLO-1", "HOLO-2", "HOLO-9"])).toBe(
    'depends_on = [ # blockers\n  "HOLO-1", # the store\n  # HOLO-3 lands after review\n  "HOLO-2", # the board\n  "HOLO-9",\n]\nestimate = 30\n');
  expect(withKey(source, "", "depends_on", ["HOLO-2"])).toBe(
    'depends_on = [ # blockers\n  # HOLO-3 lands after review\n  "HOLO-2" # the board\n]\nestimate = 30\n');
});

test("removing an item whose comma sits on a later row drops that comma and keeps the comment rows", () => {
  const source = '\tdepends_on = [\n\t\t"HOLO-1"\n\t\t# comment\n\t\t,\n\t]\n';
  const removed = withKey(source, "", "depends_on", []);
  expect(removed).toBe('\tdepends_on = [\n\t\t# comment\n\t]\n');
  expect(Bun.TOML.parse(removed)).toEqual({ depends_on: [] });
  const leading = 'depends_on = [\n  "HOLO-1"\n  , "HOLO-2" # the board\n]\n';
  const swapped = withKey(leading, "", "depends_on", ["HOLO-1", "HOLO-9"]);
  expect(Bun.TOML.parse(swapped)).toEqual({ depends_on: ["HOLO-1", "HOLO-9"] });
  expect(Bun.TOML.parse(withKey(leading, "", "depends_on", ["HOLO-2"]))).toEqual({ depends_on: ["HOLO-2"] });
});

test("removing the item whose comma opens the next row and adding another puts the new separator before that row's comment", () => {
  const source = 'depends_on = [\n  "HOLO-1"\n  , "HOLO-2"# board\n]\n';
  const swapped = withKey(source, "", "depends_on", ["HOLO-2", "HOLO-9"]);
  expect(swapped).toBe('depends_on = [\n   "HOLO-2",# board\n  "HOLO-9",\n]\n');
  expect(Bun.TOML.parse(swapped)).toEqual({ depends_on: ["HOLO-2", "HOLO-9"] });
});

test("adding an item to an array whose trailing comma shares the closing row moves that comma onto the item before it", () => {
  const source = '  depends_on = [\n    "HOLO-1"\n    # comment\n  , ]\n';
  const added = withKey(source, "", "depends_on", ["HOLO-1", "HOLO-9"]);
  expect(added).toBe('  depends_on = [\n    "HOLO-1",\n    # comment\n    "HOLO-9",\n   ]\n');
  expect(Bun.TOML.parse(added)).toEqual({ depends_on: ["HOLO-1", "HOLO-9"] });
});

test("removing an item between two kept items in a leading-comma array keeps them separated and the comment row", () => {
  const source = 'depends_on = [\n  "HOLO-1"\n  , "HOLO-2",\n  # keep this comment\n  "HOLO-3",\n]\n';
  const removed = withKey(source, "", "depends_on", ["HOLO-1", "HOLO-3"]);
  expect(removed).toContain("  # keep this comment\n");
  expect(Bun.TOML.parse(removed)).toEqual({ depends_on: ["HOLO-1", "HOLO-3"] });
});
