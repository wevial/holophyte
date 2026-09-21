import { useState } from "react";
import { useRunDetail } from "../hooks/useRunDetail";
import type { PrFacts } from "../lib/attention";
import { formatAge, formatClock } from "../lib/format";
import type { Fetch } from "../lib/poll";
import type { AttentionItem } from "../lib/types";
import { FindingCard } from "./FindingCard";
import { Markdown } from "./Markdown";

/** Mounted only for open rows, so collapsed candidates never fetch detail. */
export function PullRequestDetail({ base, id, item, now, polls, deps }: {
  base: string;
  id: number;
  item: AttentionItem;
  now: number;
  polls: number;
  deps?: { fetch: Fetch };
}) {
  const [retry, setRetry] = useState(0);
  const { detail, loading, error } = useRunDetail(base, id, polls + retry, deps);
  const rounds = [...(detail?.rounds ?? [])].sort((a, b) => a.started_ms - b.started_ms || a.round - b.round);
  const latest = rounds.at(-1);
  const activity = [...(detail?.events ?? [])].sort((a, b) => b.at - a.at)[0];
  const facts = item.pr as PrFacts | undefined;
  return <div className="space-y-3 break-words text-[13px] text-body">
    {loading && <p role="status" className="text-muted">Loading run detail…</p>}
    {error && <div className="flex flex-wrap items-center gap-2">
      <p role="alert" className="text-bad">{error}</p>
      <button type="button" className="rounded-button border border-chip-border px-2 py-1 font-semibold"
        onClick={() => setRetry(value => value + 1)}>Retry</button>
    </div>}
    {detail && <>
      <section aria-label="Latest review">
        <h4 className="font-semibold text-ink">{latest ? `Round ${rounds.length} · ${latest.verdict}` : "No review round yet"}</h4>
        {latest && <>
          <p className="text-[12px] text-muted">{latest.reviewer_model ? `${latest.reviewer_model} · ` : ""}{formatClock(latest.ended_ms ?? latest.started_ms)}</p>
          {latest.findings.length ? <ul className="mt-2 flex flex-col gap-2">
            {latest.findings.map((finding, index) => <FindingCard key={index} finding={finding} />)}
          </ul> : <p className="text-muted">No findings</p>}
        </>}
      </section>
      <section aria-label="Pull request facts">
        <h4 className="font-semibold text-ink">Pull request facts</h4>
        {typeof item.reason === "string" && <Markdown>{item.reason}</Markdown>}
        <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1">
          {Object.entries({ "Number": facts?.number, "Checks": facts?.checks, "Review": facts?.review, "Open threads": facts?.threads })
            .map(([label, value]) => <div key={label}><dt className="inline font-semibold">{label}: </dt><dd className="inline">{value ?? "unknown"}</dd></div>)}
        </dl>
        {item.pr_url && <a className="break-all text-link" href={item.pr_url} target="_blank" rel="noopener noreferrer">{item.pr_url}</a>}
      </section>
      <section aria-label="Last factory activity">
        <h4 className="font-semibold text-ink">Last factory activity</h4>
        {activity ? <>
          <Markdown>{activity.summary}</Markdown>
          <time dateTime={new Date(activity.at).toISOString()} className="text-[12px] text-muted">
            {formatClock(activity.at)} · {formatAge(Math.max(0, now - activity.at))} ago
          </time>
        </> : <p className="text-muted">No factory activity recorded</p>}
      </section>
      <a className="text-link" href={`${base}/#run=${id}`}>Open run {id}</a>
    </>}
  </div>;
}
