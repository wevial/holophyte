import { afterEach, expect, test } from "bun:test";
import { cleanup, render } from "@testing-library/react";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { Markdown } from "../../src/app/components/Markdown";

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

test("a bot comment keeps its closed native disclosure and the finding outside it", () => {
  const { container } = render(<Markdown>{comment}</Markdown>);
  const details = container.querySelector("details")!;
  expect(details).not.toBeNull();
  expect(details.open).toBe(false);
  expect(details.querySelector("summary")!.textContent).toBe("Analysis");
  expect(details.querySelector("pre code")!.textContent).toContain("rg token && echo checked");
  // Native details controls visibility, without deleting its contents.
  details.open = true;
  expect(details.open).toBe(true);
  expect(container.querySelector("strong")!.closest("details")).toBeNull();
  expect(container.querySelector("strong")!.textContent).toBe("Handle empty tokens.");
  expect(container.textContent).toContain("Reject missing tokens before dispatch.");
  expect(container.textContent).not.toContain("<details>");
  expect(container.querySelector("em")!.textContent).toBe("Data integrity");
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

test("the source tree has one renderer and never sets inner HTML", () => {
  const src = new URL("../../src/", import.meta.url);
  expect(existsSync(new URL("lib/markdown.ts", src))).toBe(false);
  for (const file of readdirSync(src, { recursive: true }).map(String).filter((file) => /\.tsx?$/.test(file))) {
    const text = readFileSync(new URL(file, src), "utf8");
    expect(text).not.toMatch(/cleanCommentBody|dangerouslySetInnerHTML|\.innerHTML\s*=/);
  }
});
