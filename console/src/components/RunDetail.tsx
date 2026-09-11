import { useRunDetail } from "../hooks/useRunDetail";
import { useRunFiles, type RunFilesState } from "../hooks/useRunFiles";
import { openFindings, severityCounts } from "../lib/findings";
import { formatClock, formatSpan } from "../lib/format";
import type { Fetch } from "../lib/poll";
import { phaseLabel } from "../lib/runs";
import { boxRemaining, buildTimeline } from "../lib/timeline";
import type { RunDetailBody } from "../lib/types";
import { ActionButton } from "./ActionButton";
import { FilesTouched } from "./FilesTouched";
import { FindingCard } from "./FindingCard";
import { RoundTimeline } from "./RoundTimeline";
import { RunLog } from "./RunLog";
import { PrLink, Sha } from "./ShippedTable";

/** "Round R of MAX": R is the rounds seen (the first one is coming while
 *  none is), MAX the loop's cap from the wire, else the rounds seen. */
export function roundLine(body: RunDetailBody): string {
  const seen = body.rounds.length;
  const current = Math.max(1, seen);
  const max = Math.max(current, body.run.max_rounds ?? seen);
  return `Round ${current} of ${max} · ${phaseLabel(body.run.phase)}`;
}

/** The expanded run's card: header line, round timeline, the newest
 *  round's open findings and the run log from `/runs/N`, the files touched
 *  from `/runs/N/files`; both read on expand and again each poll. */
export function RunDetail({
  base,
  id,
  now,
  sinceMs = 0,
  polls,
  deps,
}: {
  base: string;
  id: number;
  /** The daemon's clock at the last poll, so the timeline agrees with the row's time box. */
  now: number;
  /** Local milliseconds since that poll; the run log's summary ages by it. */
  sinceMs?: number;
  polls: number;
  deps?: { fetch: Fetch };
}) {
  const { detail, error, loading } = useRunDetail(base, id, polls, deps);
  const files = useRunFiles(base, id, polls, deps);
  return (
    <div data-detail className="pr-4 pb-[14px] pl-[44px]">
      {loading && <p className="text-[12px] text-muted">loading…</p>}
      {error && (
        <p data-detail-error className="text-[12px] font-semibold text-bad">
          {error}
        </p>
      )}
      {detail && <Card body={detail} files={files} now={now} sinceMs={sinceMs} />}
    </div>
  );
}

function Card({ body, files, now, sinceMs }: { body: RunDetailBody; files: RunFilesState; now: number; sinceMs: number }) {
  const { run, rounds } = body;
  const remaining = boxRemaining(run, now);
  const over = remaining < 0;
  const findings = openFindings(rounds);
  const counts = severityCounts(findings);
  return (
    <article aria-label={`run ${run.id}`} className="rounded-[10px] border border-line bg-card px-4 py-3">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[13px] font-semibold text-ink">{roundLine(body)}</span>
        <span data-started className="text-[12px] text-muted">
          started {formatClock(run.started_ms)}
          {run.host ? ` · ${run.host}` : ""}
        </span>
        {run.merge_sha && <Sha row={{ merge_sha: run.merge_sha, commit_url: run.commit_url }} />}
        <PrLink url={run.pr_url} />
        <span
          data-box={over ? "over" : "left"}
          className={`ml-auto font-mono text-[12px] ${over ? "font-semibold text-bad" : "text-muted"}`}
        >
          {over ? `${formatSpan(-remaining)} over the box` : `${formatSpan(remaining)} left in box`}
        </span>
      </header>
      <div className="mt-3 grid grid-cols-[1fr_280px] gap-7">
        <div className="min-w-0">
          <RoundTimeline segments={buildTimeline({ ...run, rounds, events: body.events }, now)} />
          <div className="mt-4 flex items-baseline gap-3">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">Open findings</span>
            <span data-severity-counts className="font-mono text-[12px] text-muted">
              {counts.must} must · {counts.should} should
            </span>
          </div>
          {findings.length === 0 ? (
            <p className="mt-2 text-[13px] text-muted">{rounds.length === 0 ? "No review round yet" : "No open findings"}</p>
          ) : (
            <ul className="mt-2 flex flex-col gap-2">
              {findings.map((finding, index) => (
                <FindingCard key={`${finding.path}:${finding.line ?? ""}:${index}`} finding={finding} />
              ))}
            </ul>
          )}
        </div>
        <FilesTouched files={files.files} error={files.error} status={files.status} loading={files.loading} />
      </div>
      <footer className="mt-3 flex gap-2">
        <ActionButton>Kill run</ActionButton>
        <ActionButton>Requeue ticket</ActionButton>
      </footer>
      <RunLog events={body.events} rounds={rounds} now={now + sinceMs} />
    </article>
  );
}
