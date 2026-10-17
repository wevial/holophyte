export type Declaration = { path: string[]; name: string; value: string };

export function declarations(source: string): Declaration[] {
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
      const match = /^\s*(--[a-z0-9-]+|[a-z][a-z0-9-]*)\s*:\s*([\s\S]*)$/.exec(buffer);
      if (match) out.push({ path: [...path], name: match[1]!, value: match[2]!.trim() });
      buffer = "";
    } else {
      buffer += char;
    }
  }
  return out;
}

export function tokensUnder(decls: Declaration[], selector: string, wrapper?: string): string[] {
  const names = decls
    .filter((d) => d.name.startsWith("--") && d.path.at(-1) === selector && (wrapper ? d.path.at(-2) === wrapper : d.path.length === 1))
    .map((d) => d.name);
  if (names.length === 0) throw new Error(`theme.css has no ${wrapper ? `${wrapper} ` : ""}${selector} rule`);
  return [...new Set(names)].sort();
}
