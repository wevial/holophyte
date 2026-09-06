import { CHIP_LABELS, KINDS, countsByKind, type Kind } from "../lib/attention";
import { isSupervisorStale, projectName } from "../lib/derive";
import { age } from "../lib/format";
import { hostName, hostTone, runCounts, type HostRecord } from "../lib/hosts";
import { ActionButton } from "./ActionButton";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** "1 question · 2 stale runs" from a host's own `/attention`, empty with none. */
const NOUNS: Record<Kind, string> = {
  blocked: "question",
  stale_run: "stale run",
  failed: "failed run",
  supervisor: "supervisor",
  unreachable: "unreachable",
};

function attentionSummary(host: HostRecord): string[] {
  const counts = countsByKind(host.attention?.items ?? []);
  return KINDS.filter((kind) => counts[kind] > 0).map((kind) => plural(counts[kind], NOUNS[kind] ?? CHIP_LABELS[kind]));
}

/** One daemon's card in the Hosts view: the dot, name and address, the
 *  daemon, supervisor and runs cells, its project row, and the two operator
 *  actions, rendered disabled until writes arrive. An unreachable daemon
 *  says so in the header, with its last good answer's age, in place of the
 *  three cells. `now` is the console's clock. */
export function HostPanel({ host, now }: { host: HostRecord; now: number }) {
  const { status } = host;
  const unreachable = host.error != null;
  const tone = hostTone(host);
  const dot = { ok: "bg-ok", bad: "bg-bad", faint: "bg-faint" }[tone];
  const stale = status ? isSupervisorStale(status.supervisor, status.thresholds.heartbeat_stale_ms) : false;
  const seen = host.seen_ms == null ? "never answered" : `last seen ${age(now - host.seen_ms)} ago`;
  const counts = status ? runCounts(status) : null;
  const runsCell = counts
    ? [counts.active > 0 || counts.stale === 0 ? `${counts.active} active` : null, counts.stale > 0 ? `${counts.stale} stale` : null]
        .filter((part): part is string => part != null)
        .join(" · ")
    : "";
  const supervisorCell = status
    ? [
        stale ? "stale" : status.supervisor.state,
        status.supervisor.pid == null ? null : `pid ${status.supervisor.pid}`,
        status.supervisor.heartbeat_age_ms == null ? null : `hb ${age(status.supervisor.heartbeat_age_ms)}`,
      ]
        .filter((part): part is string => part != null)
        .join(" · ")
    : "";
  const summary = [status ? plural(status.runs.length, "run") : null, ...attentionSummary(host)]
    .filter((part): part is string => part != null)
    .join(" · ");

  return (
    <article
      aria-label={hostName(host)}
      data-host={host.address}
      data-unreachable={unreachable || undefined}
      data-stale={(stale && !unreachable) || undefined}
      className={`rounded-[10px] border bg-card p-[18px] shadow-card ${tone === "bad" ? "border-bad/50" : "border-line"}`}
    >
      <header className="flex items-center gap-2.5">
        <span aria-hidden="true" className={`size-2.5 shrink-0 rounded-chip ${dot}`} />
        <span className="text-[17px] font-semibold text-ink">{hostName(host)}</span>
        <span className="truncate font-mono text-[12px] text-muted">{host.address}</span>
        {unreachable && (
          <span data-unreachable-line className="ml-auto font-mono text-[12px] font-semibold text-bad">
            unreachable · {seen}
          </span>
        )}
      </header>
      {!unreachable && status && (
        <dl className="mt-4 grid grid-cols-3 gap-4">
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Daemon</dt>
            <dd data-daemon className="mt-1 text-[13px] text-body">
              {status.daemon ? `up ${age(status.now - status.daemon.started_ms)}` : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Supervisor</dt>
            <dd
              data-supervisor={stale ? "stale" : status.supervisor.state}
              className={`mt-1 font-mono text-[13px] ${stale ? "font-semibold text-bad-text" : "text-ok-text"}`}
            >
              {supervisorCell}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Runs</dt>
            <dd data-runs className="mt-1 text-[13px] text-body">
              {runsCell}
            </dd>
          </div>
        </dl>
      )}
      {status && host.project != null && (
        <div className="mt-4 border-t border-line-faint pt-3">
          <p className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Projects</p>
          <div className="mt-1.5 flex items-baseline gap-2">
            <span className="text-[13px] font-semibold text-ink">{projectName(host.project)}</span>
            <span className="truncate font-mono text-[12px] text-muted">{host.project}</span>
            <span data-project-summary className="ml-auto shrink-0 text-[12px] text-muted">
              {summary}
            </span>
          </div>
        </div>
      )}
      <footer className="mt-4 flex gap-2">
        <ActionButton>Restart supervisor</ActionButton>
        <ActionButton>Open daemon log</ActionButton>
      </footer>
    </article>
  );
}
