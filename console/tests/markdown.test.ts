import { expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { cleanCommentBody, renderMarkdown } from "../src/lib/markdown";
import { InstructionCard } from "../src/components/InstructionCard";
import { FindingCard } from "../src/components/FindingCard";

const body = `_🗄️ Data Integrity_ | _🟡 Minor_ | _⚡ Quick win_

<details>
<summary>Analysis</summary>
Script executed
<details><summary>Nested analysis</summary>
\`\`\`bash
printf '</details>'
\`\`\`
Length of output: 100
</details>
Repository: example
</details>
<!-- private analysis
<details>hidden</details>
-->

**Handle empty tokens.**

Reject missing tokens before dispatch.`;

test("comments render category and finding without nested collapsed analysis", () => {
  const cleaned = cleanCommentBody(body);
  const outputs = [
    renderToStaticMarkup(renderMarkdown(cleaned)),
    renderToStaticMarkup(createElement(InstructionCard, { instruction: {
      kind: "instruction", path: "app.py", author: "operator", request: body, url: "",
    } })),
    renderToStaticMarkup(createElement(FindingCard, { finding: {
      path: "app.py", severity: "nit", message: body,
    } })),
  ];
  expect(cleaned).toStartWith("🗄️ Data Integrity | 🟡 Minor | ⚡ Quick win");
  for (const output of outputs) {
    expect(output).toContain("<strong>Handle empty tokens.</strong>");
    expect(output).toContain("Reject missing tokens before dispatch.");
    for (const noise of ["Script executed", "printf", "Length of output", "Repository:",
      "Nested analysis", "private analysis", "&lt;", "_🗄️"]) {
      expect(output).not.toContain(noise);
    }
  }
});

test("HTML-free prose and fenced code are preserved byte for byte", () => {
  for (const plain of ["before --> after", "_Category_\r\n\r\n**Finding** with trailing spaces.  \r\n",
    "A comparison:\n\n```js\nif (a < b) run();\n```\n",
    "~~~html\n<details>keep this example</details>\n<!-- keep -->\n~~~\n"]) {
    expect(cleanCommentBody(plain)).toBe(plain);
  }
});

// An unclosed HTML comment in an example must not consume the closing fence
// or hide the real finding that follows the collapsed analysis.
test("HTML tokens inside fences cannot change comment or disclosure state", () => {
  const code = "```html\n<!-- example\n<details>example\n```\n";
  expect(cleanCommentBody(code + "<details>Script executed</details>\nVisible finding"))
    .toBe(code + "\nVisible finding");
});

for (const [name, opening] of [["details block", "<details>"], ["HTML comment", "<!--"]]) {
  test(`an unclosed ${name} preserves trailing finding prose`, () => {
    const prose = "Analysis\n\n**Handle empty tokens.**\n\nReject missing tokens before dispatch.";
    const cleaned = cleanCommentBody(opening + prose);
    expect(cleaned).toBe(prose);
    const output = renderToStaticMarkup(renderMarkdown(cleaned));
    expect(output).toContain("<strong>Handle empty tokens.</strong>");
    expect(output).toContain("Reject missing tokens before dispatch.");
    expect(output).not.toContain("&lt;");
  });
}
