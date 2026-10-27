import { commitLink, prLabel, shortSha } from "../../lib/shipped";

const SHA_CLASS = "font-mono text-[12px] text-link";

/** The short sha in the mono teal style: an anchor to the commit on origin
 *  opening in a new tab where the daemon served `commit_url`, else plain. */
export function Sha({ row }: { row: { merge_sha: string | null; commit_url?: string | null } }) {
  const href = commitLink(row);
  const text = shortSha(row.merge_sha);
  if (href == null) {
    return (
      <span data-sha className={SHA_CLASS}>
        {text}
      </span>
    );
  }
  return (
    <a data-sha href={href} target="_blank" rel="noopener noreferrer" className={`${SHA_CLASS} hover:underline`}>
      {text}
    </a>
  );
}

/** The run's pull request as "PR #N" in the sha's link style, opening in
 *  a new tab; nothing at all when the run opened none. `title` is the
 *  anchor's tooltip: the Shipped table hands it the merge sha the cell no
 *  longer shows. */
export function PrLink({ url, title }: { url: string | null | undefined; title?: string | null }) {
  if (!url) return null;
  return (
    <a data-pr href={url} title={title ?? undefined} target="_blank" rel="noopener noreferrer" className={`${SHA_CLASS} hover:underline`}>
      {prLabel(url)}
    </a>
  );
}
