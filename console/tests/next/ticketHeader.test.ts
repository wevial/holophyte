import { expect, test } from "bun:test";
import { HeaderDate, readHeader, withKey, type Table } from "../../src/next/ticketHeader";

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
