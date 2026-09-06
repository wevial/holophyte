import logo from "../../../assets/menubar-template.svg";
import { formatDuration } from "../lib/format";
import { isSupervisorStale, portOf, projectName, supervisorLabel } from "../lib/derive";
import type { PollState } from "../lib/poll";
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
export type ProjectChoice = "all" | string;

export function Rail({
  base,
  poll,
  view,
  onView,
  project,
  onProject,
  theme,
  onTheme,
}: {
  base: string;
  poll: PollState;
  view: View;
  onView: (view: View) => void;
  project: ProjectChoice;
  onProject: (project: ProjectChoice) => void;
  theme: Theme;
  onTheme: (theme: Theme) => void;
}) {
  const { status, attention, error, polledAgo } = poll;
  const path = status ? (status.project ?? status.target) : null;
  const attentionCount = attention?.items.length ?? 0;
  const tone = !status
    ? "faint"
    : isSupervisorStale(status.supervisor, status.thresholds.heartbeat_stale_ms)
      ? "bad"
      : status.supervisor.state === "live"
        ? "ok"
        : "faint";

  return (
    <nav
      aria-label="Console"
      className="flex h-screen w-[220px] shrink-0 flex-col gap-[22px] overflow-y-auto bg-rail px-[14px] py-[18px]"
    >
      <div className="flex items-center gap-2 px-2">
        <img src={logo} alt="" width={20} height={20} className="size-5 invert" />
        <span className="text-[15px] font-bold text-paper">Holophyte</span>
      </div>

      <RailGroup label="Projects">
        <ProjectRow
          name="All projects"
          tone="faint"
          selected={project === "all"}
          onClick={() => onProject("all")}
        />
        {status && path && (
          <ProjectRow
            name={projectName(path)}
            sub={supervisorLabel(status)}
            count={status.runs.length}
            tone={tone}
            selected={project === path}
            onClick={() => onProject(path)}
          />
        )}
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
        <RailGroup label="Hosts">{status && <HostCard status={status} port={portOf(base)} />}</RailGroup>
        <p className="px-2 font-mono text-[11px] text-rail-faint">
          {polledAgo == null ? "polling…" : `polled ${formatDuration(polledAgo)} ago`}
        </p>
        {error && (
          <p role="alert" className="px-2 font-mono text-[10px] text-rail-bad-text">
            poll failed: {error}
          </p>
        )}
        <ThemeToggle theme={theme} onChange={onTheme} />
      </div>
    </nav>
  );
}
