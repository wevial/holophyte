import { expect, test } from "bun:test";
import { declarations, tokensUnder as tokensIn } from "./cssTokens";

const css = await Bun.file(new URL("../src/theme.css", import.meta.url)).text();

const decls = declarations(css);
const tokensUnder = (selector: string, wrapper?: string) => tokensIn(decls, selector, wrapper);

test("the parser distinguishes the three token blocks", () => {
  // A sanity check on the walk itself: the blocks are found by exact
  // selector, so the paper set is not read for the dark selector.
  const paperDecl = decls.find((d) => d.path.at(-1) === ":root");
  const darkDecl = decls.find((d) => d.path.at(-1) === ':root[data-theme="dark"]');
  expect(paperDecl?.path).toEqual([":root"]);
  expect(darkDecl?.path).toEqual([':root[data-theme="dark"]']);
  const probe = declarations(':root { --a: 1; --b: 2; }\n:root[data-theme="dark"] { --a: 3; }');
  expect(probe.filter((d) => d.path.at(-1) === ':root[data-theme="dark"]').map((d) => d.name)).toEqual(["--a"]);
});

test("every dark token is also defined on bare :root, and vice versa", () => {
  const paper = tokensUnder(":root");
  const dark = tokensUnder(':root[data-theme="dark"]');
  expect(dark.length).toBeGreaterThan(20);
  expect(dark.filter((name) => !paper.includes(name))).toEqual([]);
  expect(paper.filter((name) => !dark.includes(name))).toEqual([]);
});

test("the system-preference block applies the same dark set under :root:not([data-theme=\"light\"])", () => {
  const media = tokensUnder(':root:not([data-theme="light"])', "@media (prefers-color-scheme: dark)");
  expect(media).toEqual(tokensUnder(':root[data-theme="dark"]'));
});

test("every raw colour token is mapped to a Tailwind name in @theme inline", () => {
  const paper = tokensUnder(":root").filter((name) => name !== "--card-shadow");
  const themeBlock = css.slice(css.indexOf("@theme inline {"));
  const unmapped = paper.filter((name) => !themeBlock.includes(`var(${name})`));
  expect(unmapped).toEqual([]);
});
