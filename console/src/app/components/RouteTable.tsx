import { age, formatClock } from "../../lib/format";
import type { AgentRoute, HostCard } from "../hosts";

function probeText(route: AgentRoute, stale: boolean) {
  const probe = route.probe;
  if (stale) return probe
    ? `unknown · ${probe.ok ? "ok" : "failed"} at ${formatClock(probe.at_ms)}${probe.ok ? " · not probed since" : ""}`
    : "unknown · not probed yet";
  if (route.down) return "down · its fallback failed too";
  if (!probe) return "not probed yet";
  return probe.ok ? `ok · ${age(probe.age_ms)} ago · ${(probe.duration_ms / 1000).toFixed(1)}s`
    : `down · failed ${age(probe.age_ms)} ago${probe.why ? ` · ${probe.why}` : ""}`;
}

export function RouteTable({ host }: { host: HostCard }) {
  return (
    <section className="min-w-0 border-t border-rule px-4 py-4 min-[1100px]:px-5">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h3 className="font-sans text-[11px] font-semibold uppercase tracking-[0.1em] text-ink-3">Agent routes</h3>
        <span className="h-px min-w-4 flex-1 bg-rule-2" />
        <p className="font-mono text-[11px] text-ink-3">{host.down ? `as of ${host.seen ?? "an unknown time"}` : "probed when the loop starts"}</p>
      </div>
      <table aria-label={`Agent routes on ${host.name}`} className="block w-full table-fixed font-mono text-[13px] min-[700px]:table">
        <thead className="hidden text-left text-[11px] font-normal text-ink-3 min-[700px]:table-header-group">
          <tr>{["Seat", "Route", "Probe", "Fallback"].map((label) => <th key={label} className="px-3 pb-2 font-normal">{label}</th>)}</tr>
        </thead>
        <tbody className="block min-[700px]:table-row-group">
          {host.routes.map((route) => {
            const failed = route.probe?.ok === false;
            const [tone, glyph] = host.down || (!route.probe && !route.down) ? ["text-ink-3", "◷"] : failed || route.down ? ["text-bad", "×"] : ["text-live", "✓"];
            const cell = "block min-w-0 px-3 py-2 [overflow-wrap:anywhere] min-[700px]:table-cell";
            return (
              <tr key={`${route.seat}:${route.label}`} className={`mb-3 block border-l-2 min-[700px]:table-row ${failed ? "border-bad bg-bad-wash" : "border-transparent"}`}>
                <th scope="row" className={`${cell} text-left font-normal text-ink`}>{route.seat}{route.projects.length < host.projects.length ? ` · ${route.projects.join(", ")}` : ""}</th>
                <td className={`${cell} text-ink-2`}>{route.label}</td>
                <td className={`${cell} text-ink-2`}><span aria-hidden="true" className={tone}>{glyph} </span><span>{probeText(route, host.down)}</span></td>
                <td className={`${cell} text-ink-2`}>{route.fallbacks.map((fallback) => <div key={fallback.label}>
                  {fallback.label}{fallback.inUse && <span className="ml-2 inline-block border border-wait px-1.5 py-0.5 text-[11px] text-wait">in use · recorded</span>}
                </div>)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
