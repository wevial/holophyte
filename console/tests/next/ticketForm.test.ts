import { fileURLToPath } from "node:url";
import { expect, test } from "bun:test";
import TEMPLATE from "../../../ticketTemplate.md" with { type: "text" };
import { blankForm, composeBody, fieldOf, parseBody, withField } from "../../src/next/ticketForm";
import { adjacentHtmlBody, inlineHtmlBody } from "./ticketBodies";
import { adjacentFencesBody, adjacentMarkdownBody, annotatedFieldsBody, commentedCriteriaBody, closedCommentsBody, continuedHowBody, detailedBody, fencedBody, labeledCommentBody, mockupBody, multilineInlineCodeBody, unclosedCommentBody, windowsBody } from "./ticketBodies";


function checkerFieldsWithoutComments(body: string) {
  const result = Bun.spawnSync(["python3", "-c", `
import json, sys
from ticket_template import COMMENT_RE, parse
ticket = parse(COMMENT_RE.sub("", sys.stdin.read()))
print(json.dumps({"summary": ticket.summary, "criteria": ticket.acceptance,
                  "how": ticket.how, "estimate": ticket.estimate_min}))
`], { cwd: fileURLToPath(new URL("../../../", import.meta.url)), stdin: Buffer.from(body) });
  expect(result.exitCode).toBe(0);
  return JSON.parse(result.stdout.toString()) as { summary: string; criteria: string[]; how: string; estimate: number | null };
}

test("What/Why/How edits preserve adjacent HTML blocks byte for byte", () => {
  for (const block of ["<div>Keep this warning.</div>", "   <SECTION class=\"warning\">Keep this warning.</SECTION>", "</table>", "<script>keepWarning()</script>", "<?keep warning?>", "<!DOCTYPE html>", "<![CDATA[Keep this warning.]]>"]) {
    for (const eol of ["\n", "\r\n"]) {
      const body = adjacentHtmlBody.replaceAll("<div>Keep this warning.</div>", block).replaceAll("\n", eol);
      const form = parseBody(body);
      for (const [field, prose] of [["what", "Show the record."], ["why", "Explain the record."], ["how", "Read the record."]] as const) {
        expect(fieldOf(form, field)).toBe(prose);
        expect(composeBody(withField(form, field, "Read once."))).toBe(body.replace(prose, "Read once."));
      }
    }
  }
});

test("inline HTML and autolinks remain part of a prose paragraph", () => {
  const form = parseBody(inlineHtmlBody);
  const prose = "Read the record.\n<span>Keep reading.</span>\n<https://example.com/record>";
  expect(fieldOf(form, "how")).toBe(prose);
  expect(composeBody(withField(form, "how", "Read once."))).toBe(inlineHtmlBody.replace(prose, "Read once."));
});

test("What/Why/How edits preserve adjacent fenced blocks outside the prose paragraph", () => {
  const cases = [
    ["what", "Original outcome."],
    ["why", "Original reason."],
    ["how", "Original prose."],
  ] as const;
  for (const body of [adjacentFencesBody, adjacentFencesBody.replaceAll("\n", "\r\n")]) {
    const form = parseBody(body);
    for (const [field, prose] of cases) {
      expect(fieldOf(form, field)).toBe(prose);
      expect(composeBody(withField(form, field, "Revised prose."))).toBe(
        body.replace(prose, "Revised prose."),
      );
    }
  }
});

test("parsing and composing preserve template, extensions, CRLF, and fence contents byte for byte", () => {
  for (const body of [TEMPLATE, detailedBody, windowsBody, fencedBody, "", "No headings or final newline", "## Summary"]) {
    expect(composeBody(parseBody(body))).toBe(body);
  }
  expect(parseBody(fencedBody).sections.map((section) => section.name)).toEqual(["Summary"]);
  expect(parseBody(windowsBody).sections.map((section) => section.name)).toEqual(["Summary", "Verify command(s)"]);
});

test("criteria edits replace only unchecked items and preserve reviewer rules and all other sections", () => {
  const original = parseBody(detailedBody);
  expect(fieldOf(original, "criteria")).toEqual([
    "Saved notes appear.", "Empty notes stay hidden.", "Existing run actions still work.",
  ]);
  const edited = withField(original, "criteria", ["Notes appear on the card.", "No notes means no card."]);
  expect(composeBody(edited)).toBe(detailedBody.replace(
    "- [ ] Saved notes appear.\n- [ ] Empty notes stay hidden.\n- [ ] Existing run actions still work.",
    "- [ ] Notes appear on the card.\n- [ ] No notes means no card.",
  ));
  expect(composeBody(original)).toBe(detailedBody);
});

test("UI metadata inserts after How and a missing mock-up uses template section order", () => {
  const original = parseBody(detailedBody);
  expect(fieldOf(original, "uiChange")).toBeNull();
  const marked = withField(original, "uiChange", "major");
  expect(composeBody(marked)).toContain("**How:** Read the stored notes.\n\n**UI change:** major\n");
  const linked = withField(marked, "mockupLink", "https://claude.ai/artifact/abc");
  expect(fieldOf(linked, "mockupLink")).toBe("https://claude.ai/artifact/abc");
  expect(linked.sections.map((section) => section.name)).toEqual([
    "Summary", "What / Why / How", "Reproduce", "Mock-up", "In scope", "Out of scope",
    "Acceptance criteria", "Verify command(s)", "Contract checks", "Implementation notes",
    "Estimate & dependencies", "Open questions",
  ]);
  expect(composeBody(withField(parseBody(mockupBody), "uiChange", null))).toBe(
    mockupBody.replace("**UI change:** major\n", ""),
  );
  expect(composeBody(withField(linked, "uiChange", null))).not.toContain("**UI change:**");
});

test("estimate and dependency edits retain orchestration and serialize an empty dependency list as none", () => {
  const original = parseBody(detailedBody);
  expect(fieldOf(original, "estimate")).toBe(30);
  expect(fieldOf(original, "dependsOn")).toEqual(["HOLO-100", "HOLO-101"]);
  const edited = withField(withField(original, "estimate", 45), "dependsOn", ["HOLO-238"]);
  expect(composeBody(edited)).toBe(detailedBody.replace(
    "Estimate: 30 min · Depends on: HOLO-100, HOLO-101",
    "Estimate: 45 min · Depends on: HOLO-238",
  ));
  expect(composeBody(withField(edited, "dependsOn", []))).toBe(detailedBody.replace(
    "Estimate: 30 min · Depends on: HOLO-100, HOLO-101", "Estimate: 45 min · Depends on: none",
  ));
});

test("a blank form keeps only required sections, clears guidance, and exposes the summary hint", () => {
  const form = blankForm();
  const body = composeBody(withField(form, "title", "Steer notes show on the run detail card"));
  expect(body.startsWith("# Steer notes show on the run detail card\n")).toBe(true);
  expect(form.sections.map((section) => section.name)).toEqual([
    "Summary", "What / Why / How", "In scope", "Out of scope", "Acceptance criteria",
    "Verify command(s)", "Implementation notes", "Estimate & dependencies", "Open questions",
  ]);
  expect(body).not.toContain("<!--");
  expect(body).not.toMatch(/<[^>]*>/);
  expect(body).not.toMatch(/^>/m);
  expect(fieldOf(form, "verify")).toEqual([]);
  expect(fieldOf(form, "openQuestions")).toBe("- None");
  expect(form.hints.summary).toBe("Describe the outcome in one or two sentences.");
  expect(form.hints.openQuestions ?? "").toBe("");
});

test("text field edits preserve neighboring paragraphs, labels, and section whitespace", () => {
  const original = parseBody(detailedBody);
  const cases = [
    ["title", "Steer notes", "New title"],
    ["summary", "Show the operator's notes.", "Show all notes."],
    ["what", "Show notes on the run card.\nContinue the same paragraph.", "Show a notes panel."],
    ["why", "Operators need context.", "Operators need history."],
    ["how", "Read the stored notes.", "Read the run record."],
    ["notes", "Use the existing store.", "Keep the existing renderer."],
    ["openQuestions", "- None", "- Which renderer?"],
  ] as const;
  for (const [field, before, after] of cases) {
    expect(fieldOf(original, field)).toBe(before);
    expect(composeBody(withField(original, field, after))).toBe(detailedBody.replace(before, after));
  }
});

test("scope lists replace only their items and retain annotations", () => {
  const original = parseBody(detailedBody);
  expect(fieldOf(original, "inScope")).toEqual(["Run card", "Stored notes"]);
  expect(fieldOf(original, "outOfScope")).toEqual(["Notifications"]);
  expect(composeBody(withField(original, "inScope", ["Notes panel"]))).toBe(
    detailedBody.replace("- Run card\n- Stored notes", "- Notes panel"),
  );
  expect(composeBody(withField(original, "outOfScope", []))).toBe(
    detailedBody.replace("- Notifications\n", ""),
  );
});

test("verify reads and replaces only the first fence including command lines that look like headings", () => {
  const original = parseBody(detailedBody);
  expect(fieldOf(original, "verify")).toEqual([
    "bun test", "## This is a command comment, not a section", "printf 'done'",
  ]);
  expect(composeBody(withField(original, "verify", ["bun run typecheck", "bun test"]))).toBe(
    detailedBody.replace("bun test\n## This is a command comment, not a section\nprintf 'done'", "bun run typecheck\nbun test"),
  );
  expect(composeBody(withField(parseBody(windowsBody), "verify", ["bun run lint"]))).toBe(
    windowsBody.replace("## Inside the fence\r\nbun test", "bun run lint"),
  );
});

test("mock-up fields edit independently while preserving design notes and other media", () => {
  const original = parseBody(mockupBody);
  expect(fieldOf(original, "mockupLink")).toBe("https://claude.ai/artifact/original");
  expect(fieldOf(original, "mockupApproval")).toBe("Approved 2026-10-10: card layout");
  expect(fieldOf(original, "mockupImages")).toEqual([
    "![Overview](https://assets.example/overview.png)", "![Detail](https://assets.example/detail.png)",
  ]);
  expect(composeBody(withField(original, "mockupLink", "https://claude.ai/artifact/revised"))).toBe(
    mockupBody.replace("https://claude.ai/artifact/original", "https://claude.ai/artifact/revised"),
  );
  expect(composeBody(withField(original, "mockupApproval", "Approved 2026-10-17: revised card"))).toBe(
    mockupBody.replace("Approved 2026-10-10: card layout", "Approved 2026-10-17: revised card"),
  );
  expect(composeBody(withField(original, "mockupImages", ["![New](https://assets.example/new.png)"]))).toBe(
    mockupBody.replace("![Overview](https://assets.example/overview.png)\n![Detail](https://assets.example/detail.png)", "![New](https://assets.example/new.png)"),
  );
  expect(composeBody(withField(original, "uiChange", "minor"))).toBe(mockupBody.replace("**UI change:** major", "**UI change:** minor"));
});

test("editing a blank summary preserves the following section boundary", () => {
  const edited = withField(blankForm(), "summary", "New summary");
  const reparsed = parseBody(composeBody(edited));
  expect(fieldOf(reparsed, "summary")).toBe("New summary");
  expect(reparsed.sections.map((section) => section.name)).toEqual([
    "Summary", "What / Why / How", "In scope", "Out of scope", "Acceptance criteria",
    "Verify command(s)", "Implementation notes", "Estimate & dependencies", "Open questions",
  ]);
});

test("setting an empty field inserts its absent section in template order", () => {
  const original = parseBody(mockupBody);
  const summary = withField(original, "summary", "");
  expect(summary.sections.map((section) => section.name)).toEqual([
    "Summary", "What / Why / How", "Mock-up", "In scope",
  ]);
  const criteria = withField(original, "criteria", []);
  expect(criteria.sections.map((section) => section.name)).toEqual([
    "What / Why / How", "Mock-up", "In scope", "Acceptance criteria",
  ]);
});

test("prose edits include bold continuation and preserve adjacent Markdown blocks", () => {
  for (const block of ["- Keep the existing database.", "* Keep the database.", "+ Keep the database.", "1. Keep the database.", "1) Keep the database.", "### Keep the heading", "   > Keep the quote", "---", "***", "___"]) {
    for (const eol of ["\n", "\r\n"]) {
      const body = adjacentMarkdownBody.replace("- Keep the existing database.", block).replaceAll("\n", eol);
      const form = parseBody(body);
      expect(fieldOf(form, "how")).toBe(`Read the record.${eol}**Keep** reading.`);
      expect(composeBody(withField(form, "how", "Read the updated record."))).toBe(
        body.replace(`Read the record.${eol}**Keep** reading.`, "Read the updated record."),
      );
    }
  }
});

test("criteria edits exclude HTML comments and keep replacement items visible", () => {
  for (const eol of ["\n", "\r\n"]) {
    const body = commentedCriteriaBody.replaceAll("\n", eol);
    const form = parseBody(body);
    expect(fieldOf(form, "criteria")).toEqual(["Visible criterion"]);
    const edited = composeBody(withField(form, "criteria", ["Replacement criterion"]));
    expect(edited).toBe(body.replace("Visible criterion", "Replacement criterion"));
    expect(fieldOf(parseBody(edited), "criteria")).toEqual(["Replacement criterion"]);
  }
});


test("a criterion with a trailing HTML comment reads and edits only visible text", () => {
  for (const eol of ["\n", "\r\n"]) {
    const body = annotatedFieldsBody.replaceAll("\n", eol);
    const form = parseBody(body);
    expect(fieldOf(form, "criteria")).toEqual(["Saved notes appear."]);
    expect(fieldOf(form, "criteria")).toEqual(checkerFieldsWithoutComments(body).criteria);
    expect(composeBody(withField(form, "criteria", ["Updated notes appear."]))).toBe(
      body.replace("Saved notes appear.", "Updated notes appear."),
    );
  }
});

test("a How line with a trailing HTML comment reads and edits only visible text", () => {
  for (const eol of ["\n", "\r\n"]) {
    const body = annotatedFieldsBody.replaceAll("\n", eol);
    const form = parseBody(body);
    expect(fieldOf(form, "how")).toBe("Read the stored notes.");
    expect(fieldOf(form, "how")).toBe(checkerFieldsWithoutComments(body).how);
    expect(composeBody(withField(form, "how", "Read the run record."))).toBe(
      body.replace("Read the stored notes.", "Read the run record."),
    );
  }
});

test.each([
  ["an unclosed HTML comment in Summary", unclosedCommentBody, "Render the literal <!-- in the editor."],
  ["a two-line inline code span with an unclosed comment", multilineInlineCodeBody, "Show `the opening marker\nvalue <!--` literally."],
])("%s leaves later fields visible", (_name, body, summary) => {
  const form = parseBody(body);
  const checker = checkerFieldsWithoutComments(body);
  expect(fieldOf(form, "summary").replace(/\s+/g, " ")).toBe(checker.summary);
  expect(fieldOf(form, "criteria")).toEqual(checker.criteria);
  expect(fieldOf(form, "estimate")).toBe(checker.estimate);
  expect(fieldOf(form, "summary")).toBe(summary);
  expect(fieldOf(form, "criteria")).toEqual(["Keep the criterion."]);
  expect(fieldOf(form, "estimate")).toBe(30);
  expect(composeBody(withField(form, "summary", "New summary."))).toBe(
    body.replace(summary, "New summary."),
  );
});

test("How paragraphs continue after trailing spaces and UI insertion keeps How's annotation on its line", () => {
  const form = parseBody(continuedHowBody);
  expect(fieldOf(form, "how")).toBe("Read the record.  \nContinue reading.");
  expect(composeBody(withField(form, "how", "Read once."))).toBe(
    continuedHowBody.replace("Read the record.  \nContinue reading.", "Read once."),
  );
  expect(composeBody(withField(parseBody(annotatedFieldsBody), "uiChange", "major"))).toBe(
    annotatedFieldsBody.replace("**How:** Read the stored notes. <!-- keep explanation -->", "**How:** Read the stored notes. <!-- keep explanation -->\n\n**UI change:** major"),
  );
});

test("closed comments end at the first closing marker even inside inline code and fences", () => {
  const form = parseBody(closedCommentsBody);
  expect(composeBody(form)).toBe(closedCommentsBody);
  expect(fieldOf(form, "summary")).toBe("Show `` literally.  Visible tail.");
  expect(fieldOf(form, "verify")).toEqual(["printf 'start'", "printf 'done'"]);
});

test("a comment between a field label and its value survives a visible-text edit", () => {
  const form = parseBody(labeledCommentBody);
  expect(fieldOf(form, "how")).toBe("Read the record.");
  expect(composeBody(withField(form, "how", "Read the updated record."))).toBe(
    labeledCommentBody.replace("Read the record.", "Read the updated record."),
  );
});

test("verify edits retain comments inside a fence with its closing marker on its own line", () => {
  const form = parseBody(closedCommentsBody);
  for (const commands of [["printf 'new'"], []]) {
    const edited = composeBody(withField(form, "verify", commands));
    expect(edited).toBe(closedCommentsBody.replace(
      "printf 'start'<!-- hidden\nstill hidden -->\nprintf 'done'",
      commands.join("\n") + "<!-- hidden\nstill hidden -->",
    ));
    expect(fieldOf(parseBody(edited), "verify")).toEqual(commands);
    expect(fieldOf(parseBody(edited), "openQuestions")).toBe("- None");
  }
});
