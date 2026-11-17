import type { ReactNode } from "react";
import { age, formatClock } from "../../lib/format";
import type { AgentRoute, HostCard } from "../hosts";

const time = (value: string) => <span className="font-mono text-[12.5px]">{value}</span>;

function probeText(route: AgentRoute, stale: boolean): ReactNode {
  const probe = route.probe;
  if (stale) return probe
    ? <>unknown · {probe.ok ? "ok" : "failed"} at {time(formatClock(probe.at_ms))}{probe.ok ? " · not probed since" : ""}</>
    : "unknown · not probed yet";
  if (route.down) return "down · its fallback failed too";
  if (!probe) return route.probeWhen === "inherits" ? `uses ${route.inherits ?? "another seat's"} route`
    : route.probeWhen === "first_use" ? "probed on first use" : "not probed yet";
  return probe.ok ? <>ok · {time(age(probe.age_ms))} ago · {time(`${(probe.duration_ms / 1000).toFixed(1)}s`)}</>
    : <>down · failed {time(age(probe.age_ms))} ago{probe.why ? ` · ${probe.why}` : ""}</>;
}

export function RouteTable({ host }: { host: HostCard }) {
  return (
    <section className="min-w-0 border-t border-hair px-4 py-4 min-[1100px]:px-5">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="font-sans text-[13px] font-semibold text-ink">Agent routes</h3>
        <p className="font-sans text-[13px] text-ink-3">{host.down ? `as of ${host.seen ?? "an unknown time"}` : "probed when the loop starts"}</p>
      </div>
      <table aria-label={`Agent routes on ${host.name}`} className="block w-full table-fixed border-collapse font-sans text-[13px] min-[700px]:table">
        <thead className="hidden text-left min-[700px]:table-header-group">
          <tr>{["Seat", "Route", "Probe", "Fallback"].map((label) => <th key={label} className="h-[30px] border-b border-hair px-2.5 text-[12px] font-medium text-ink-3">{label}</th>)}</tr>
        </thead>
        <tbody className="block min-[700px]:table-row-group">
          {host.routes.map((route) => {
            const failed = route.probe?.ok === false;
            const deferred = !host.down && !route.probe && !route.down && (route.probeWhen === "inherits" || route.probeWhen === "first_use");
            const [tone, glyph] = deferred ? ["text-ink-3", ""] : host.down || (!route.probe && !route.down) ? ["text-ink-3", "◷"] : failed || route.down ? ["text-bad", "×"] : ["text-live", "✓"];
            const cell = "block min-w-0 px-2.5 py-1 [overflow-wrap:anywhere] min-[700px]:table-cell min-[700px]:h-[34px] min-[700px]:border-b min-[700px]:border-hair min-[700px]:py-1.5 min-[700px]:align-middle";
            return (
              <tr key={`${route.seat}:${route.label}`} className={`block border-b border-hair py-1.5 last:border-b-0 transition-colors duration-(--motion-colour) min-[700px]:table-row min-[700px]:border-b-0 min-[700px]:py-0 ${failed ? "bg-bad-wash" : "hover:bg-hover"}`}>
                <th scope="row" className={`${cell} text-left font-medium text-ink`}>{route.seat}{route.projects.length < host.projects.length ? ` · ${route.projects.join(", ")}` : ""}</th>
                <td className={`${cell} font-mono text-[12.5px] text-ink-2`}>{route.label}</td>
                <td className={`${cell} ${deferred ? "text-ink-3" : "text-ink-2"}`}>{glyph && <span aria-hidden="true" className={tone}>{glyph} </span>}<span>{probeText(route, host.down)}</span></td>
                <td className={`${cell} text-ink-2`}>{route.fallbacks.map((fallback) => <div key={fallback.label} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-mono text-[12.5px]">{fallback.label}</span>{fallback.inUse && <span className="inline-flex h-5 items-center rounded-[5px] bg-wait-wash px-[7px] font-sans text-[11.5px] font-semibold text-wait">in use · recorded</span>}
                </div>)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
