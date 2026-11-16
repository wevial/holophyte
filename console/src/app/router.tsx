import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  redirect,
  RouterProvider,
} from "@tanstack/react-router";
import { defaultPollDeps, type PollDeps } from "../lib/poll";
import { applyTextSize, useHeaderEffect, useTextSize } from "./appearance";
import { CommandPalette } from "./components/CommandPalette";
import { Haze } from "./components/Haze";
import { Sidebar, TitleBar, useSidebarFold } from "./components/Sidebar";
import { ToastProvider } from "./components/Toast";
import { TokenPrompt } from "./components/TokenPrompt";
import { validateFindingsSearch } from "./findings";
import { useFleet } from "./fleet";
import { SettingsMarksProvider } from "./settingsDraft";
import { Findings } from "./pages/Findings";
import { FlowPage, validateFlowSearch } from "./pages/Flow";
import { MergeQueuePage } from "./pages/MergeQueue";
import { validateRunsSearch } from "./history";
import { NotFound } from "./pages/Placeholder";
import { readStoredScope } from "./projects";
import { Overview } from "./pages/Overview";
import { TurnPage } from "./pages/Turn";
import { RunPage } from "./pages/Run";
import { Runs } from "./pages/Runs";
import { ScoutPage } from "./pages/Scout";
import { Scouts } from "./pages/Scouts";
import { Settings } from "./pages/Settings";
import { StoriesIndex, Story } from "./pages/Story";
import { TicketPage } from "./pages/Ticket";
import { NewTicket } from "./pages/NewTicket";
import { EditTicket } from "./pages/EditTicket";
import { Tickets } from "./pages/Tickets";
import { validateScoutListSearch, validateScoutsSearch } from "./scouts";
import { validateTicketsSearch } from "./tickets";
import { OriginContext, PeersContext, PollDepsContext, validateScope } from "./scope";

function Shell() {
  const sidebar = useSidebarFold();
  const { effect } = useHeaderEffect();
  const { textSize } = useTextSize();
  useLayoutEffect(() => applyTextSize(textSize), [textSize]);
  const [palette, setPalette] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (palette) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      event.preventDefault();
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setPalette(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [palette]);
  return (
    <ToastProvider>
      <SettingsMarksProvider>
      <div data-shell="" className="flex min-h-screen flex-col bg-side text-ink min-[720px]:h-screen min-[720px]:overflow-hidden">
        <TitleBar {...sidebar} />
        <div className="flex flex-1 flex-col min-[720px]:min-h-0 min-[720px]:flex-row">
          <Sidebar folded={sidebar.folded} />
          <main className="relative isolate min-w-0 flex-1 bg-canvas min-[720px]:overflow-y-auto min-[720px]:rounded-tl-[10px] min-[720px]:shadow-[0_0_0_1px_var(--rule-2)]">
            <TokenPrompt />
            <Outlet />
            {effect === "haze" && <Haze />}
          </main>
        </div>
      </div>
      {palette && <CommandPalette onClosed={() => setPalette(false)} returnTo={() => opener.current} />}
      </SettingsMarksProvider>
    </ToastProvider>
  );
}

const rootRoute = createRootRoute({
  validateSearch: validateScope,
  component: Shell,
  notFoundComponent: NotFound,
});

function pageRoute<TPath extends string>(path: TPath, page: () => ReactNode) {
  return createRoute({ getParentRoute: () => rootRoute, path, component: page });
}

function redirectHistory({ project, outcome }: Record<string, unknown>): never {
  const state = outcome === "merged" || outcome === "failed" || outcome === "abandoned" ? outcome : undefined;
  throw redirect({ to: "/runs", search: { project: typeof project === "string" ? project : undefined, state, range: "30d" }, replace: true });
}

const routeTree = rootRoute.addChildren([
  pageRoute("/", Overview),
  createRoute({ getParentRoute: () => rootRoute, path: "/runs", validateSearch: validateRunsSearch, component: Runs }),
  createRoute({ getParentRoute: () => rootRoute, path: "/runs/history", beforeLoad: ({ location }) => redirectHistory(location.search) }),
  createRoute({ getParentRoute: () => rootRoute, path: "/runs/$project/$run", component: RunPage }),
  pageRoute("/merge-queue", MergeQueuePage),
  createRoute({ getParentRoute: () => rootRoute, path: "/tickets", validateSearch: validateTicketsSearch, component: Tickets }),
  pageRoute("/tickets/$project/new", NewTicket),
  createRoute({ getParentRoute: () => rootRoute, path: "/tickets/$project/$key", component: TicketPage }),
  pageRoute("/tickets/$project/$key/edit", EditTicket),
  createRoute({ getParentRoute: () => rootRoute, path: "/runs/$project/$run/turns/$turn", component: TurnPage }),
  pageRoute("/stories", StoriesIndex),
  createRoute({ getParentRoute: () => rootRoute, path: "/stories/$project/$key", component: Story }),
  createRoute({ getParentRoute: () => rootRoute, path: "/findings", validateSearch: validateFindingsSearch, component: Findings }),
  createRoute({ getParentRoute: () => rootRoute, path: "/scouts", validateSearch: validateScoutsSearch, component: Scouts }),
  createRoute({ getParentRoute: () => rootRoute, path: "/scouts/$project/$scout", validateSearch: validateScoutListSearch, component: ScoutPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/flow", validateSearch: validateFlowSearch, component: FlowPage }),
  pageRoute("/settings", Settings),
  pageRoute("/settings/$tab", Settings),
]);

function createConsoleRouter() {
  const bare = window.location.hash === "" || window.location.hash === "#";
  const router = createRouter({ routeTree, history: createHashHistory() });
  const stored = bare ? readStoredScope() : undefined;
  if (stored) void router.navigate({ to: "/", search: { project: stored }, replace: true });
  return router;
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createConsoleRouter>;
  }
  interface HistoryState {
    draft?: string;
    scout?: number;
    revision?: number;
  }
}

export function ConsoleApp({
  base = window.location.origin,
  pollDeps = defaultPollDeps,
}: {
  base?: string;
  pollDeps?: PollDeps;
}) {
  const peers = useFleet(base, pollDeps);
  const [router] = useState(createConsoleRouter);
  return (
    <OriginContext.Provider value={base}>
      <PollDepsContext.Provider value={pollDeps}>
        <PeersContext.Provider value={peers}>
          <RouterProvider router={router} />
        </PeersContext.Provider>
      </PollDepsContext.Provider>
    </OriginContext.Provider>
  );
}
