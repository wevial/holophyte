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

const urlTransform = (url: string, key: string) => key === "src"
  ? (imageSourceAllowed(url) ? url : undefined)
  : defaultUrlTransform(url);

const INLINE_ELEMENTS = ["p", "code", "strong", "em", "a", "br"];
const INLINE_CODE = "rounded-[6px] bg-selected px-1 font-mono text-[12.5px]";
const INLINE_LINK = "text-act underline";

/** All untrusted Markdown becomes sanitized React nodes, inside the theme. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="ticket-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
        urlTransform={urlTransform}
        components={{
          a: ({ children, href, title }) => <a href={href} title={title} target="_blank" rel="noreferrer noopener">{children}</a>,
          img: ({ src, alt, title, width, height }) => typeof src === "string" && imageSourceAllowed(src)
            ? <img src={src} alt={alt} title={title} width={width} height={height} /> : null,
        }}
      >{children}</ReactMarkdown>
    </div>
  );
}

/** Sanitized inline Markdown for single-row fields such as criteria. */
export function InlineMarkdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
      urlTransform={urlTransform}
      allowedElements={INLINE_ELEMENTS}
      unwrapDisallowed
      components={{
        p: ({ children }) => <>{children}</>,
        code: ({ children }) => <code className={INLINE_CODE}>{children}</code>,
        a: ({ children, href, title }) => (
          <a href={href} title={title} target="_blank" rel="noreferrer noopener" className={INLINE_LINK}>
            {children}
          </a>
        ),
      }}
    >{children}</ReactMarkdown>
  );
}
