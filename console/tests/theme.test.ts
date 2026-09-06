import { expect, test } from "bun:test";

const css = await Bun.file(new URL("../src/theme.css", import.meta.url)).text();

type Declaration = { path: string[]; name: string };

/** A minimal walk over the stylesheet: every `name: value;` declaration
 *  with the preludes of the blocks enclosing it, outermost first, so an
 *  `@media` wrapper and the selector inside it are both visible. */
function declarations(source: string): Declaration[] {
  const text = source.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Declaration[] = [];
  const path: string[] = [];
  let buffer = "";
  for (const char of text) {
    if (char === "{") {
      path.push(buffer.trim());
      buffer = "";
    } else if (char === "}") {
      path.pop();
      buffer = "";
    } else if (char === ";") {
      const match = /^\s*(--[a-z0-9-]+)\s*:/.exec(buffer);
      if (match) out.push({ path: [...path], name: match[1]! });
      buffer = "";
    } else {
      buffer += char;
    }
  }
  return out;
}

const decls = declarations(css);

/** The custom-property names declared directly in the rule whose selector
 *  is exactly `selector` (optionally under the given at-rule wrapper). */
function tokensUnder(selector: string, wrapper?: string): string[] {
  const names = decls
    .filter((d) => d.path.at(-1) === selector && (wrapper ? d.path.at(-2) === wrapper : d.path.length === 1))
    .map((d) => d.name);
  if (names.length === 0) throw new Error(`theme.css has no ${wrapper ? `${wrapper} ` : ""}${selector} rule`);
  return [...new Set(names)].sort();
}

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
