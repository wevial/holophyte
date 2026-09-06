import { useState } from "react";
import { Hosts } from "./components/Hosts";
import { Now } from "./components/Now";
import { Shipped } from "./components/Shipped";
import { Rail, VIEWS, type ProjectChoice, type View } from "./components/Rail";
import { usePeers } from "./hooks/usePeers";
import { visibleHosts } from "./lib/hosts";
import { defaultPollDeps, REQUEST_TIMEOUT_MS, type PollDeps } from "./lib/poll";
import { applyTheme, readTheme, writeTheme, type Theme } from "./lib/theme";

/** The page polls the daemon that served it, and through its `/peers`
 *  every other daemon it names. */
function servingBase(): string {
  return window.location.origin;
}

export function App({
  base = servingBase(),
  pollDeps = defaultPollDeps,
  timeoutMs = REQUEST_TIMEOUT_MS,
}: {
  base?: string;
  pollDeps?: PollDeps;
  timeoutMs?: number;
}) {
  const [view, setView] = useState<View>("now");
  const [project, setProject] = useState<ProjectChoice>("all");
  const [theme, setTheme] = useState<Theme>(readTheme);
  const peers = usePeers(base, pollDeps, timeoutMs);
  const { hosts, now, polls } = peers;

  const chooseTheme = (next: Theme) => {
    setTheme(next);
    writeTheme(next);
    applyTheme(next);
  };

  const answered = hosts.some((host) => host.status != null);
  const shownBases = visibleHosts(hosts, project)
    .filter((host) => host.status != null)
    .map((host) => host.base);
  const daemonNow = hosts.reduce((latest, host) => Math.max(latest, host.status?.now ?? 0), 0) || pollDeps.now();
  const heading = VIEWS.find((candidate) => candidate.id === view)?.label ?? view;
  return (
    <div className="flex h-screen overflow-hidden bg-paper font-sans text-ink">
      <Rail
        peers={peers}
        view={view}
        onView={setView}
        project={project}
        onProject={setProject}
        theme={theme}
        onTheme={chooseTheme}
      />
      <main className="min-w-0 flex-1 overflow-y-auto">
        {view !== "shipped" && view !== "hosts" && (
          <h1 className="px-6 pt-6 pb-4 text-2xl font-semibold">{heading}</h1>
        )}
        {view === "shipped" ? (
          <Shipped bases={shownBases} now={daemonNow} polls={polls} deps={pollDeps} />
        ) : view === "hosts" ? (
          <Hosts hosts={hosts} project={project} now={now} />
        ) : view === "now" && answered ? (
          <Now hosts={hosts} project={project} now={now} polls={polls} deps={pollDeps} />
        ) : (
          <p className="px-6 text-[13px] text-muted">Nothing to show here yet.</p>
        )}
      </main>
    </div>
  );
}
