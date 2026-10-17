import { useId } from "react";
import { useHostsTab, type HostCard } from "../hosts";

const tones = { live: "text-live", need: "text-need", bad: "text-bad", "ink-3": "text-ink-3" };
const mono = "font-mono text-[12px] text-ink-3";

function LastReport({ host }: { host: HostCard }) {
  const names = host.projects.map((project) => project.name);
  const held = names.length === 0 ? "it holds no projects, so no run is waiting on it"
    : `it holds ${names.join(" and ")}; their runs are as of ${host.seen ?? "an unknown time"}`;
  return (
    <div role="status" className="border-l-2 border-need bg-need-wash px-4 py-3">
      <p className="text-[14px] text-need"><span aria-hidden="true">△ </span>{host.name} {host.seen ? `has not answered since ${host.seen}.` : "has never answered."}</p>
      <p className={`${mono} mt-1`}>{host.seen ? `everything below is its last report · ${held}` : "No last report is available"} · nothing here can act on it until it answers</p>
    </div>
  );
}

function DaemonCard({ host }: { host: HostCard }) {
  const id = useId();
  return (
    <article aria-labelledby={id} className="min-w-0 border border-rule bg-paper">
      {host.down && <LastReport host={host} />}
      <header className="p-4 min-[1100px]:p-5">
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <h2 id={id} className="font-sans text-[19px] font-semibold text-ink">{host.name}</h2>
          <p className={`font-mono text-[12px] ${tones[host.health.tone]}`}><span aria-hidden="true">{host.health.glyph} </span>{host.health.text}</p>
        </div>
        <p className={`${mono} mt-3 break-words leading-6`}>
          <span className="text-ink">{host.address}</span> · build {host.build}
          {host.checkout && <span className="text-wait"> · checkout at {host.checkout}</span>}
          {" · "}up {host.uptime} · last sweep {host.lastSweep}
        </p>
      </header>
      <div className="grid grid-cols-1 border-t border-rule min-[1100px]:grid-cols-[1.25fr_1fr_1fr]">
        <section aria-label="Projects" className="min-w-0 p-4 min-[1100px]:p-5">
          <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">Projects</h3>
          {host.projects.length === 0 ? <p className="text-[13px] text-ink-2">None. A spare host: it answers sweeps but claims nothing.</p> :
            <ul className="space-y-3">{host.projects.map((project) => (
              <li key={project.name} className="text-[13px] leading-6">
                <span aria-hidden="true" className={`mr-2 inline-block h-2 w-2 ${project.admission === "enabled" ? "bg-live" : project.admission === "held" ? "bg-wait" : "bg-ink-3"}`}
                  style={project.admission === "held" ? { backgroundImage: "repeating-linear-gradient(135deg, transparent, transparent 2px, var(--paper) 2px, var(--paper) 3px)" } : undefined} />
                <span className="break-words underline decoration-rule underline-offset-4">{project.name}</span>{" "}
                <span className="text-ink-2">{project.admission === "enabled" ? "taking work" : project.admission ?? "disabled"} · {project.workers} {project.workers === 1 ? "worker" : "workers"}</span>
                {project.error && <p className="break-words font-mono text-[12px] text-bad">{project.error}</p>}
              </li>
            ))}</ul>}
        </section>
        <section aria-label="Supervisor" className="min-w-0 border-t border-rule p-4 min-[1100px]:border-l min-[1100px]:border-t-0 min-[1100px]:p-5">
          <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">Supervisor</h3>
          <p className={`text-[16px] ${tones[host.supervisorTone]}`}>{host.supervisor}</p>
          <p className={`${mono} mt-2 leading-5`}>{host.supervisorNote}</p>
        </section>
        <section aria-label="Sweep" className="min-w-0 border-t border-rule p-4 min-[1100px]:border-l min-[1100px]:border-t-0 min-[1100px]:p-5">
          <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">Sweep</h3>
          <p className={`text-[16px] ${tones[host.sweepTone]}`}>{host.sweep}</p>
          {host.sweepNote && <p className={`${mono} mt-2 leading-5`}>{host.sweepNote}</p>}
        </section>
      </div>
    </article>
  );
}

export function HostsTab() {
  const { summary, daemons, allDown } = useHostsTab();
  return (
    <div className="mt-6 max-w-[1180px] space-y-4">
      <p className="font-mono text-[13px] text-ink-2">{summary}</p>
      {allDown && <div role="status" className="border-l-2 border-need bg-need-wash p-4">
        <p className="text-[14px] text-need">No host is answering this console.</p>
        <p className={`${mono} mt-1`}>last answer from {daemons.map((host) => `${host.name} ${host.seen ? `at ${host.seen}` : "never answered"}`).join(" · ")}</p>
      </div>}
      {daemons.map((host) => <DaemonCard key={host.key} host={host} />)}
      <footer className={`${mono} border-b border-rule pb-4 pt-2`}>the token stays in this browser</footer>
    </div>
  );
}
