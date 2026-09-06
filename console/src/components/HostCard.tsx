import { age } from "../lib/format";
import { hostName, hostTone, type HostRecord } from "../lib/hosts";

/** One daemon's card in the rail footer. A stale supervisor pulses the
 *  red dot; an unreachable daemon shows a static one and "unreachable"
 *  where the heartbeat was, with the last good answer's age beneath.
 *  `now` is the console's clock, for "last seen". */
export function HostCard({ host, now }: { host: HostRecord; now: number }) {
  const { status } = host;
  const unreachable = host.error != null;
  const tone = hostTone(host);
  const bad = tone === "bad";
  const stale = !unreachable && bad;
  const heartbeat =
    status?.supervisor.heartbeat_age_ms == null ? "no hb" : `hb ${age(status.supervisor.heartbeat_age_ms)}`;
  const port = /:\d+$/.exec(host.address)?.[0] ?? "";
  const runs = status ? `${status.runs.length} ${status.runs.length === 1 ? "run" : "runs"}` : null;
  const uptime = status?.daemon ? `daemon up ${age(status.now - status.daemon.started_ms)}` : null;
  const seen = host.seen_ms == null ? "never answered" : `last seen ${age(now - host.seen_ms)} ago`;
  const second = unreachable ? [seen, runs] : [uptime, runs];
  return (
    <div
      data-host={host.address}
      data-stale={stale || undefined}
      data-unreachable={unreachable || undefined}
      className={`rounded-card bg-rail-card p-2.5 ${bad ? "border border-bad/50" : ""}`}
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className={`size-2 shrink-0 rounded-chip ${
            stale ? "animate-[pulse-dot_1.2s_ease-in-out_infinite] bg-bad" : bad ? "bg-bad" : tone === "ok" ? "bg-ok" : "bg-rail-faint"
          }`}
        />
        <span className="truncate text-[13px] font-semibold text-rail-text">{hostName(host)}</span>
        <span data-heartbeat className={`ml-auto font-mono text-[11px] ${bad ? "text-rail-bad-text" : "text-rail-sub"}`}>
          {unreachable ? "unreachable" : `${port} · ${heartbeat}`}
        </span>
      </div>
      <div className="mt-1 font-mono text-[10px] text-rail-faint">
        {second.filter((part): part is string => part != null).join(" · ")}
      </div>
    </div>
  );
}
