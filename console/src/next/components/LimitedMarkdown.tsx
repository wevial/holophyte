import type { Root, RootContent } from "mdast";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function imageText() {
  const walk = (node: Root | RootContent) => {
    if (!("children" in node)) return;
    node.children = node.children.map((child) => child.type === "image" || child.type === "imageReference"
      ? { type: "text" as const, value: child.alt ?? "" }
      : child) as typeof node.children;
    node.children.forEach(walk);
  };
  return (tree: Root) => walk(tree);
}

export function LimitedMarkdown({ children, lines }: { children: string; lines: 2 | 4 }) {
  const [open, setOpen] = useState(false);
  if (!children.trim()) return null;
  return (
    <div className="min-w-0">
      <div className={`break-words [&_p+p]:mt-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_code]:font-mono [&_a]:underline ${open ? "" : lines === 4 ? "line-clamp-4" : "line-clamp-2"}`}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm, imageText]}
          allowedElements={["p", "strong", "em", "code", "ul", "ol", "li", "a", "br"]}
          unwrapDisallowed
          components={{ a: ({ children, href, title }) => <a href={href} title={title} target="_blank" rel="noreferrer">{children}</a> }}
        >{children}</ReactMarkdown>
      </div>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="mt-1 text-xs text-ink-3 hover:text-ink">
        {open ? "Show less" : "Show all"}
      </button>
    </div>
  );
}
