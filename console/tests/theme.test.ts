import { expect, test } from "bun:test";

const css = await Bun.file(new URL("../src/theme.css", import.meta.url)).text();

/** The custom-property names declared in the block that opens right after
 *  `selector` (the first `{` after it, up to its matching `}`). */
function tokensUnder(selector: string): string[] {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`theme.css has no ${selector}`);
  const open = css.indexOf("{", start);
  let depth = 0;
  let end = open;
  for (; end < css.length; end += 1) {
    if (css[end] === "{") depth += 1;
    if (css[end] === "}") depth -= 1;
    if (depth === 0) break;
  }
  const names = [...css.slice(open, end).matchAll(/(--[a-z0-9-]+)\s*:/g)].map((match) => match[1]!);
  return [...new Set(names)].sort();
}

test("every dark token is also defined on bare :root", () => {
  const paper = tokensUnder("\n:root {");
  const dark = tokensUnder(':root[data-theme="dark"]');
  expect(dark.length).toBeGreaterThan(20);
  const missing = dark.filter((name) => !paper.includes(name));
  expect(missing).toEqual([]);
  const darkOnly = paper.filter((name) => !dark.includes(name));
  expect(darkOnly).toEqual([]);
});

test("the system-preference block applies the same dark set under :root:not([data-theme=\"light\"])", () => {
  const media = css.indexOf("@media (prefers-color-scheme: dark)");
  expect(media).toBeGreaterThan(-1);
  const inner = css.indexOf(':root:not([data-theme="light"])', media);
  expect(inner).toBeGreaterThan(media);
  expect(tokensUnder(':root:not([data-theme="light"])')).toEqual(tokensUnder(':root[data-theme="dark"]'));
});

test("every raw colour token is mapped to a Tailwind name in @theme inline", () => {
  const paper = tokensUnder("\n:root {").filter((name) => name !== "--card-shadow");
  const theme = css.slice(css.indexOf("@theme inline"));
  const unmapped = paper.filter((name) => !theme.includes(`var(${name})`));
  expect(unmapped).toEqual([]);
});
