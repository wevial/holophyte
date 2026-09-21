import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { Markdown } from "../src/components/Markdown";
import { FindingCard } from "../src/components/FindingCard";
import { InstructionCard } from "../src/components/InstructionCard";
import { TicketSheet } from "../src/components/TicketSheet";
import { settle } from "./harness";

afterEach(cleanup);

const comment = `_Data integrity_ | _Minor_

<details>
<summary>Analysis</summary>

\`\`\`sh
rg token && echo checked
\`\`\`

</details>

**Handle empty tokens.**

Reject missing tokens before dispatch.`;

test("bot comments preserve a closed native disclosure and visible finding across cards", () => {
  for (const component of [
    <Markdown>{comment}</Markdown>,
    <FindingCard finding={{ path: "app.py", severity: "nit", message: comment, raw: comment }} />,
    <InstructionCard instruction={{ kind: "instruction", path: "app.py", author: "bot", request: comment, url: "" }} />,
  ]) {
    const { container, queryByRole } = render(component);
    const check = (root: Element) => {
      const details = root.querySelector("details")!;
      expect(details).not.toBeNull();
      expect(details.open).toBe(false);
      expect(details.querySelector("summary")!.textContent).toBe("Analysis");
      expect(details.querySelector("pre code")!.textContent).toContain("rg token && echo checked");
      // Native details controls visibility, without deleting its contents.
      details.open = true;
      expect(details.open).toBe(true);
      expect(root.querySelector("strong")!.closest("details")).toBeNull();
      expect(root.querySelector("strong")!.textContent).toBe("Handle empty tokens.");
      expect(root.textContent).toContain("Reject missing tokens before dispatch.");
      expect(root.textContent).not.toContain("<details>");
      expect(root.querySelector("em")!.textContent).toBe("Data integrity");
    };
    check(container);
    const original = queryByRole("button", { name: "Show original comment" });
    if (original) {
      fireEvent.click(original);
      check(container.querySelector("[data-original]")!);
    }
    cleanup();
  }
});

test("hostile HTML loses scripts, handlers, styles, unsafe links and remote images", () => {
  const { container } = render(<Markdown>{`Before

<script>alert('bad')</script>
<style>p { display: none }</style>
<p onclick="alert(1)" onmouseover="alert(2)" style="display:none">Surrounding text</p>
<a href="javascript:alert(1)">unsafe link</a>
<img src="https://remote.example/track.png" onerror="alert(1)">
<img src="//remote.example/track.png">
<picture><source srcset="https://remote.example/track.png"><img src="https://remote.example/other.png"></picture>

[Safe link](https://example.com)

After`}</Markdown>);
  expect(container.querySelector("script, style, img, source, [style], [onclick], [onmouseover], [onerror]")).toBeNull();
  expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  expect(container.textContent).not.toContain("alert('bad')");
  for (const text of ["Before", "Surrounding text", "unsafe link", "After"]) expect(container.textContent).toContain(text);
  const link = container.querySelector('a[href="https://example.com"]')!;
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toContain("noopener");
});

test("images allow data and same-origin URLs only, including URL normalization", () => {
  const previous = window.location.href;
  window.location.href = "https://console.example/";
  const origin = window.location.origin;
  const { container } = render(<Markdown>{`![data](data:image/png;base64,aGVsbG8=)
![relative](/badge.png)
![same](${origin}/badge.png)
![remote](https://remote.example/image.png)
<img src="/\\remote.example/image.png">
<img src="https://remote.example/image.png" srcset="/badge.png 1x">
`}</Markdown>);
  expect([...container.querySelectorAll("img")].map((img) => img.alt)).toEqual(["data", "relative", "same"]);
  expect(container.querySelector("[srcset]")).toBeNull();
  window.location.href = previous;
});

test("an unclosed disclosure preserves the following paragraph", () => {
  const { container } = render(<Markdown>{"<details><summary>Analysis</summary>\n\nFollowing paragraph survives."}</Markdown>);
  expect(container.querySelector("details p")!.textContent).toBe("Following paragraph survives.");
});

test("the ticket sheet renders GFM structure under the theme class", async () => {
  const body = "# Heading\n\n- [ ] pending\n- [x] done\n\n```sh\nbun test\n```\n\n| Name | State |\n| --- | --- |\n| Test | done |\n\n- parent\n  - child\n\n> Quoted text";
  const { container } = render(<TicketSheet host={{ base: "http://writer:7710" }}
    card={{ key: "ticket", project: "project", runId: null, strikesMax: 3, askedMs: null, now: 1, ticket: "KO-552", title: "Markdown", status: "ready", run: null, question: null, waitsOn: [] }}
    onClose={() => {}} deps={{ fetch: async () => Response.json({ ticket: "KO-552", title: "Markdown", status: "ready", body,
      acceptance_criteria: [], verification_commands: [], time_box_ms: 1800000, run: null, mirrored_ms: 1 }) }} />);
  await act(settle);
  const root = container.querySelector("[data-sheet-body] .ticket-body")!;
  expect(root).not.toBeNull();
  expect(root.querySelector("h1")!.textContent).toBe("Heading");
  expect([...root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].map((box) => [box.checked, box.disabled])).toEqual([[false, true], [true, true]]);
  expect(root.querySelectorAll("li.task-list-item").length).toBe(2);
  expect(root.querySelector("pre code")!.textContent).toBe("bun test\n");
  expect(root.querySelector("table tbody td")!.textContent).toBe("Test");
  expect(root.querySelector("li ul li")!.textContent).toBe("child");
  expect(root.querySelector("blockquote")!.textContent).toContain("Quoted text");
});

test("the source tree has one renderer and never sets inner HTML", () => {
  const src = new URL("../src/", import.meta.url);
  expect(existsSync(new URL("lib/markdown.ts", src))).toBe(false);
  for (const file of readdirSync(src, { recursive: true }).map(String).filter((file) => /\.tsx?$/.test(file))) {
    const text = readFileSync(new URL(file, src), "utf8");
    expect(text).not.toMatch(/cleanCommentBody|dangerouslySetInnerHTML|\.innerHTML\s*=/);
  }
});
