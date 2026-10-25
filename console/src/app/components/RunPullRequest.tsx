import { PrFacts } from "../../components/PrFacts";
import type { Fact } from "../../lib/attention";
import type { RunDetail } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { commitLink, prLabel, shortSha } from "../../lib/shipped";
import { SECTION_LABEL } from "./Criteria";

const LINK = "text-ink underline decoration-rule underline-offset-2 hover:decoration-ink";

type Merge = Pick<RunDetail["run"], "merge_sha" | "commit_url" | "ended_ms">;

/** What holds the pull request after its number: a park's facts, with
 *  "ready for your merge" once its readiness says so; a merge's sha and
 *  time; else nothing. */
function Holds({ facts, ready, merge }: { facts: Fact[] | null; ready: boolean; merge: Merge | null }) {
  if (merge != null) {
    const sha = shortSha(merge.merge_sha);
    const href = commitLink(merge);
    if (!sha) return null;
    return (
      <>
        {" · merged as "}
        {href == null ? sha : <a href={href} target="_blank" rel="noreferrer" className={LINK}>{sha}</a>}
        {merge.ended_ms != null && ` at ${formatClock(merge.ended_ms)}`}
      </>
    );
  }
  return (
    <>
      {facts?.length ? (
        <>
          {" · "}
          <span className="inline [&_[data-facts]]:inline">
            <PrFacts facts={facts} form="line" />
          </span>
        </>
      ) : null}
      {ready && <span className="text-live">{" · ready for your merge"}</span>}
    </>
  );
}

/** The run's pull request on one mono line: its number linking to it, then
 *  what holds it. */
export function RunPullRequest({ url, facts, ready, merge }: { url: string; facts: Fact[] | null; ready: boolean; merge: Merge | null }) {
  return (
    <section aria-label="Pull request" data-run-pr>
      <div className={SECTION_LABEL}>Pull request</div>
      <p className="m-0 mt-2.5 font-mono text-[12px] leading-[1.6] text-ink-3 [overflow-wrap:anywhere]">
        <a href={url} target="_blank" rel="noreferrer" className={LINK}>
          {prLabel(url)}
        </a>
        <Holds facts={facts} ready={ready} merge={merge} />
      </p>
    </section>
  );
}
