import { age } from "../lib/format";
import { projectName } from "../lib/derive";
import { hostTone, type HostRecord } from "../lib/hosts";

/** The glyph the rail shows for a daemon waiting on its serve token. */
export const KEY_GLYPH = "\u26bf";

/** The text after a row's port: the run count when the daemon is
 *  healthy, else what is wrong with it. A fresh heartbeat says nothing
 *  about itself; a stale one is named with its age. */
export function rowTail(host: HostRecord): string | null {
  const { status } = host;
  if (host.error != null) return "no answer";
  if (host.needs_token) return null;
  if (!status) return "no answer";
  if (hostTone(host) === "bad") {
    const hb = status.supervisor.heartbeat_age_ms;
    return hb == null ? "supervisor stale" : `supervisor stale ${age(hb)}`;
  }
  return `${status.runs.length} ${status.runs.length === 1 ? "run" : "runs"}`;
}

/** One daemon's row in a host card: dot, project name, port in mono, then
 *  the run count or what is wrong. Clicking selects the daemon's project. */
export function HostRow({ host, selected, onClick }: { host: HostRecord; selected: boolean; onClick: () => void }) {
  const unreachable = host.error != null;
  const tone = hostTone(host);
  const bad = tone === "bad";
  const stale = !unreachable && bad;
  const port = /:\d+$/.exec(host.address)?.[0] ?? "";
  const name = host.project != null ? projectName(host.project) : host.address;
  const tail = rowTail(host);
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      data-host={host.address}
      data-stale={stale || undefined}
      data-unreachable={unreachable || undefined}
      data-needs-token={host.needs_token || undefined}
      className={`flex w-full items-center gap-2 rounded-button px-1 py-0.5 text-left ${
        selected ? "bg-rail-selected" : "hover:bg-rail-selected/50"
      }`}
    >
      <span
        aria-hidden="true"
        className={`size-2 shrink-0 rounded-chip ${
          stale ? "animate-[pulse-dot_1.2s_ease-in-out_infinite] bg-bad" : bad ? "bg-bad" : tone === "ok" ? "bg-ok" : "bg-rail-faint"
        }`}
      />
      <span data-project className="truncate text-[13px] font-semibold text-rail-text">
        {name}
      </span>
      <span data-port className="shrink-0 font-mono text-[11px] text-rail-faint">
        {port}
      </span>
      <span data-tail className={`ml-auto shrink-0 font-mono text-[11px] ${bad ? "text-rail-bad-text" : "text-rail-sub"}`}>
        {host.needs_token ? (
          <span role="img" aria-label="needs token" title="needs token">
            {KEY_GLYPH}
          </span>
        ) : (
          tail
        )}
      </span>
    </button>
  );
}
