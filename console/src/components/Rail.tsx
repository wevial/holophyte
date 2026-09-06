import logo from "../../../assets/menubar-template.svg";
import { formatDuration } from "../lib/format";
import { projectName, supervisorLabel } from "../lib/derive";
import type { ProjectChoice } from "../lib/attention";
import { hostItems, hostTone, type HostRecord, type HostTone } from "../lib/hosts";
import type { PeersState } from "../hooks/usePeers";
import type { Theme } from "../lib/theme";
import { HostCard } from "./HostCard";
import { ProjectRow } from "./ProjectRow";
import { RailGroup } from "./RailGroup";
import { ThemeToggle } from "./ThemeToggle";
import { ViewButton } from "./ViewButton";

export type View = "now" | "board" | "hosts" | "shipped";
export const VIEWS: { id: View; label: string }[] = [
  { id: "now", label: "Now" },
  { id: "board", label: "Board" },
  { id: "hosts", label: "Hosts" },
  { id: "shipped", label: "Shipped" },
];

/** `all`, or the selected project's path. */
export type { ProjectChoice } from "../lib/attention";

interface ProjectEntry {
  path: string;
  name: string;
  sub: string;
  count: number;
  tone: HostTone;
}

/** One row per project a host has answered for, in host order; a second
 *  host serving the same path pools its runs under the first. */
export function projectRows(hosts: HostRecord[]): ProjectEntry[] {
  const rows: ProjectEntry[] = [];
  for (const host of hosts) {
    if (!host.status || host.project == null) continue;
    const existing = rows.find((row) => row.path === host.project);
    if (existing) {
      existing.count += host.status.runs.length;
      continue;
    }
    rows.push({
      path: host.project,
      name: projectName(host.project),
      sub: supervisorLabel(host.status),
      count: host.status.runs.length,
      tone: host.error != null ? "faint" : hostTone(host),
    });
  }
  return rows;
}

export function Rail({
  peers,
  view,
  onView,
  project,
  onProject,
  theme,
  onTheme,
}: {
  peers: PeersState;
  view: View;
  onView: (view: View) => void;
  project: ProjectChoice;
  onProject: (project: ProjectChoice) => void;
  theme: Theme;
  onTheme: (theme: Theme) => void;
}) {
  const { hosts, polledAgo, now } = peers;
  const attentionCount = hosts.reduce((total, host) => total + hostItems(host, now).length, 0);
  const failures = hosts.filter((host) => host.error != null);

  return (
    <nav
      aria-label="Console"
      className="flex h-full w-[220px] shrink-0 flex-col gap-[22px] overflow-y-auto bg-rail px-[14px] py-[18px]"
    >
      <div className="flex items-center gap-2 px-2">
        <img src={logo} alt="" width={20} height={20} className="size-5 invert" />
        <span className="text-[15px] font-bold text-rail-fg">Holophyte</span>
      </div>

      <RailGroup label="Projects">
        <ProjectRow
          name="All projects"
          tone="faint"
          selected={project === "all"}
          onClick={() => onProject("all")}
        />
        {projectRows(hosts).map((row) => (
          <ProjectRow
            key={row.path}
            name={row.name}
            sub={row.sub}
            count={row.count}
            tone={row.tone}
            selected={project === row.path}
            onClick={() => onProject(row.path)}
          />
        ))}
      </RailGroup>

      <RailGroup label="Views">
        {VIEWS.map(({ id, label }) => (
          <ViewButton
            key={id}
            selected={view === id}
            onClick={() => onView(id)}
            badge={
              id === "now" && attentionCount > 0 ? (
                <span
                  aria-label={`${attentionCount} needing you`}
                  className="rounded-chip bg-warn px-1.5 font-mono text-[11px] leading-4 text-badge-text"
                >
                  {attentionCount}
                </span>
              ) : undefined
            }
          >
            {label}
          </ViewButton>
        ))}
      </RailGroup>

      <div className="mt-auto flex flex-col gap-2">
        <RailGroup label="Hosts">
          {hosts.map((host) => (
            <HostCard key={host.address} host={host} now={now} />
          ))}
        </RailGroup>
        <p className="px-2 font-mono text-[11px] text-rail-faint">
          {polledAgo == null ? "polling…" : `polled ${formatDuration(polledAgo)} ago`}
        </p>
        {failures.map((host) => (
          <p key={host.address} role="alert" className="px-2 font-mono text-[10px] text-rail-bad-text">
            poll failed: {host.error}
          </p>
        ))}
        <ThemeToggle theme={theme} onChange={onTheme} />
      </div>
    </nav>
  );
}
