import { boxFill, boxRatio, commitLink, deltaLabel, deltaTone, minutesLabel, prLabel, shortSha } from "../../lib/shipped";

const FILLS = { ok: "bg-ok", warn: "bg-warn", over: "bg-bad" };
const DELTA = { ok: "text-ok-text", over: "text-bad-text", muted: "text-muted" };

/** Actual against the time box: a 6px bar green at or under 80 %, amber
 *  above, red past the box, then "18m / 30m" and the signed delta. */
export function ActualVsBox({ actualMin, estimateMin }: { actualMin: number; estimateMin: number | null }) {
  const tone = boxRatio(actualMin, estimateMin);
  const delta = deltaLabel(actualMin, estimateMin);
  return (
    <span className="flex items-center gap-2">
      <span
        role="progressbar"
        aria-valuenow={Math.round(boxFill(actualMin, estimateMin) * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        data-tone={tone ?? "none"}
        className="block h-[6px] w-[92px] shrink-0 overflow-hidden rounded-[3px] bg-track"
      >
        {tone && (
          <span className={`block h-full ${FILLS[tone]}`} style={{ width: `${boxFill(actualMin, estimateMin) * 100}%` }} />
        )}
      </span>
      <span data-minutes className="font-mono text-[11px] text-muted">
        {minutesLabel(actualMin, estimateMin)}
      </span>
      {delta && (
        <span data-delta={deltaTone(actualMin, estimateMin)} className={`font-mono text-[11px] ${DELTA[deltaTone(actualMin, estimateMin)]}`}>
          {delta}
        </span>
      )}
    </span>
  );
}

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
