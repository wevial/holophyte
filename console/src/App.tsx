import { useState } from "react";
import { Rail, VIEWS, type ProjectChoice, type View } from "./components/Rail";
import { defaultPollDeps, usePoll, type PollDeps } from "./lib/poll";
import { applyTheme, readTheme, writeTheme, type Theme } from "./lib/theme";

/** The page polls the daemon that served it. */
function servingBase(): string {
  return window.location.origin;
}

export function App({ base = servingBase(), pollDeps = defaultPollDeps }: { base?: string; pollDeps?: PollDeps }) {
  const [view, setView] = useState<View>("now");
  const [project, setProject] = useState<ProjectChoice>("all");
  const [theme, setTheme] = useState<Theme>(readTheme);
  const poll = usePoll(base, pollDeps);

  const chooseTheme = (next: Theme) => {
    setTheme(next);
    writeTheme(next);
    applyTheme(next);
  };

  const heading = VIEWS.find((candidate) => candidate.id === view)?.label ?? view;
  return (
    <div className="flex h-screen overflow-hidden bg-paper font-sans text-ink">
      <Rail
        base={base}
        poll={poll}
        view={view}
        onView={setView}
        project={project}
        onProject={setProject}
        theme={theme}
        onTheme={chooseTheme}
      />
      <main className="min-w-0 flex-1 overflow-y-auto p-6">
        <h1 className="text-2xl font-semibold">{heading}</h1>
        <p className="mt-2 text-[13px] text-muted">Nothing to show here yet.</p>
      </main>
    </div>
  );
}
