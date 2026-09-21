import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

// Parse raw HTML before sanitizing. Never allow styles, event handlers or
// responsive image sources to bypass the image URL policy below.
const schema = {
  ...defaultSchema,
  tagNames: [...new Set([...(defaultSchema.tagNames ?? []), "details", "summary"])]
    .filter((tag) => tag !== "source"),
  attributes: Object.fromEntries(Object.entries(defaultSchema.attributes ?? {}).map(([tag, attributes]) => [
    tag,
    attributes.filter((attribute) => {
      const name = typeof attribute === "string" ? attribute : attribute[0];
      return !/^on/i.test(name) && name !== "style" && name !== "srcSet";
    }),
  ])),
  strip: [...(defaultSchema.strip ?? []), "style"],
  protocols: { ...defaultSchema.protocols, src: ["http", "https", "data"] },
};

function imageSourceAllowed(src: string): boolean {
  try {
    const base = typeof window === "undefined" ? undefined : window.location.href;
    const url = new URL(src, base);
    if (url.protocol === "data:") return true;
    return base !== undefined && (url.protocol === "http:" || url.protocol === "https:") &&
      url.origin === new URL(base).origin;
  } catch {
    return false;
  }
}

/** All untrusted Markdown becomes sanitized React nodes, inside the theme. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="ticket-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
        urlTransform={(url, key) => key === "src"
          ? (imageSourceAllowed(url) ? url : undefined)
          : defaultUrlTransform(url)}
        components={{
          a: ({ children, href, title }) => <a href={href} title={title} target="_blank" rel="noreferrer noopener">{children}</a>,
          img: ({ src, alt, title, width, height }) => typeof src === "string" && imageSourceAllowed(src)
            ? <img src={src} alt={alt} title={title} width={width} height={height} /> : null,
        }}
      >{children}</ReactMarkdown>
    </div>
  );
}
