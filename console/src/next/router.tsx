import { useState, type ReactNode } from "react";
import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
  useParams,
} from "@tanstack/react-router";
import { defaultPollDeps, type PollDeps } from "../lib/poll";
import { classicHref } from "../lib/view";
import { Sidebar } from "./components/Sidebar";
import { ToastProvider } from "./components/Toast";
import { TokenPrompt } from "./components/TokenPrompt";
import { useFleet } from "./fleet";
import { Findings } from "./pages/Findings";
import { validateRunsSearch } from "./history";
import { NotFound, Placeholder } from "./pages/Placeholder";
import { readStoredScope } from "./projects";
import { Overview } from "./pages/Overview";
import { Runs } from "./pages/Runs";
import { Story } from "./pages/Story";
import { Tickets } from "./pages/Tickets";
import { OriginContext, PeersContext, PollDepsContext, validateScope } from "./scope";

function Shell() {
  return (
    <ToastProvider>
      <div className="flex min-h-screen flex-col bg-canvas text-ink min-[720px]:h-screen min-[720px]:flex-row min-[720px]:overflow-hidden">
        <Sidebar />
        <main className="min-w-0 flex-1 min-[720px]:overflow-y-auto">
          <TokenPrompt />
          <Outlet />
        </main>
      </div>
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

function TicketPlaceholder() {
  const { key } = useParams({ from: "/tickets/$project/$key" });
  return (
    <Placeholder
      title={key}
      sentence="The ticket page comes here."
      classic={{ href: classicHref("#view=board"), label: "Open the Board in the classic console" }}
    />
  );
}

const routeTree = rootRoute.addChildren([
  pageRoute("/", Overview),
  createRoute({ getParentRoute: () => rootRoute, path: "/runs", validateSearch: validateRunsSearch, component: Runs }),
  pageRoute("/tickets", Tickets),
  createRoute({ getParentRoute: () => rootRoute, path: "/tickets/$project/$key", component: TicketPlaceholder }),
  createRoute({ getParentRoute: () => rootRoute, path: "/stories/$project/$key", component: Story }),
  pageRoute("/findings", Findings),
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
}

export function NextConsole({
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
