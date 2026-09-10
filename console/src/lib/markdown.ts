import { createElement, Fragment, type ReactNode } from "react";

/**
 * A deliberately small Markdown renderer for a mirrored ticket's body:
 * headings, paragraphs, bullet and checkbox lists, fenced and inline
 * code, bold and links. It returns React nodes, never an HTML string,
 * so every character the body carries is text by construction and a
 * body cannot inject markup. Anything the renderer does not recognise is
 * a paragraph of literal text.
 */

/** The block a line loop has grouped so far. */
type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "code"; lines: string[] }
  | { kind: "list"; items: { checked: boolean | null; text: string }[] }
  | { kind: "paragraph"; lines: string[] };

const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
// A fence is a line opening with ``` or ~~~, or a line that is a lone
// backtick: the ticket template's contract writes a fence that way, and
// a bare backtick means nothing else in prose.
const FENCE = /^\s*(?:```|~~~|`\s*$)/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const CHECKBOX = /^\[([ xX])\]\s*(.*)$/;

/** The body split into blocks: fenced code swallows every line until a
 *  closing fence (or the end), consecutive bullets form one list, other
 *  consecutive non-blank lines form one paragraph. */
export function blocksOf(body: string): Block[] {
  const blocks: Block[] = [];
  let code: string[] | null = null;
  // The block the next line may join: an open list or paragraph.
  let last: Block | null = null;
  for (const raw of body.split(/\r?\n/)) {
    if (code != null) {
      if (FENCE.test(raw)) code = null;
      else code.push(raw);
      continue;
    }
    if (FENCE.test(raw)) {
      code = [];
      blocks.push({ kind: "code", lines: code });
      last = null;
      continue;
    }
    if (raw.trim() === "") {
      last = null;
      continue;
    }
    const heading = HEADING.exec(raw);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1]!.length, text: heading[2]! });
      last = null;
      continue;
    }
    const bullet = BULLET.exec(raw);
    if (bullet) {
      const box = CHECKBOX.exec(bullet[1]!);
      const item = box ? { checked: box[1] !== " ", text: box[2]! } : { checked: null, text: bullet[1]! };
      if (last?.kind === "list") last.items.push(item);
      else {
        last = { kind: "list", items: [item] };
        blocks.push(last);
      }
      continue;
    }
    if (last?.kind === "paragraph") last.lines.push(raw.trim());
    else {
      last = { kind: "paragraph", lines: [raw.trim()] };
      blocks.push(last);
    }
  }
  return blocks;
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\((?:https?:\/\/|mailto:)[^\s)]+\))/;
const LINK = /^\[([^\]]+)\]\(([^\s)]+)\)$/;

/** One line of prose as nodes: inline code, bold and `[text](http…)`
 *  links; the rest stays text. A link is only made for an http(s) or
 *  mailto target, so a body cannot smuggle a script URL. */
export function inline(text: string, key = "i"): ReactNode[] {
  const nodes: ReactNode[] = [];
  let rest = text;
  let index = 0;
  while (rest.length > 0) {
    const match = INLINE.exec(rest);
    if (!match) {
      nodes.push(rest);
      break;
    }
    if (match.index > 0) nodes.push(rest.slice(0, match.index));
    const token = match[0];
    const k = `${key}${index++}`;
    if (match[1]) nodes.push(createElement("code", { key: k }, token.slice(1, -1)));
    else if (match[2]) nodes.push(createElement("strong", { key: k }, token.slice(2, -2)));
    else {
      const link = LINK.exec(token)!;
      nodes.push(createElement("a", { key: k, href: link[2], target: "_blank", rel: "noreferrer noopener" }, link[1]));
    }
    rest = rest.slice(match.index + token.length);
  }
  return nodes;
}

function renderBlock(block: Block, key: string): ReactNode {
  switch (block.kind) {
    case "heading":
      return createElement(`h${block.level}`, { key }, inline(block.text, key));
    case "code":
      return createElement("pre", { key }, createElement("code", null, block.lines.join("\n")));
    case "list":
      return createElement(
        "ul",
        { key },
        block.items.map((item, at) =>
          createElement(
            "li",
            { key: `${key}.${at}`, "data-checked": item.checked == null ? undefined : String(item.checked) },
            item.checked == null
              ? inline(item.text, `${key}.${at}`)
              : [
                  createElement("input", { key: "box", type: "checkbox", checked: item.checked, disabled: true, readOnly: true }),
                  " ",
                  ...inline(item.text, `${key}.${at}`),
                ],
          ),
        ),
      );
    case "paragraph":
      return createElement(
        "p",
        { key },
        block.lines.flatMap((line, at) => (at === 0 ? inline(line, `${key}.${at}`) : [createElement("br", { key: `${key}.br${at}` }), ...inline(line, `${key}.${at}`)])),
      );
  }
}

/** The body rendered as React nodes. */
export function renderMarkdown(body: string): ReactNode {
  return createElement(Fragment, null, blocksOf(body).map((block, at) => renderBlock(block, `b${at}`)));
}
