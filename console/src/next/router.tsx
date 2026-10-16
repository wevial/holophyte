import { useState, type ReactNode } from "react";
import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { defaultPollDeps, type PollDeps } from "../lib/poll";
import { classicHref } from "../lib/view";
import { Sidebar } from "./components/Sidebar";
import { ToastProvider } from "./components/Toast";
import { TokenPrompt } from "./components/TokenPrompt";
import { useFleet } from "./fleet";
import { NotFound, Placeholder } from "./pages/Placeholder";
import { readStoredScope } from "./projects";
import { Overview } from "./pages/Overview";
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

function placeholderRoute<TPath extends string>(path: TPath, page: Parameters<typeof Placeholder>[0]) {
  return pageRoute(path, () => <Placeholder {...page} />);
}

const routeTree = rootRoute.addChildren([
  pageRoute("/", Overview),
  placeholderRoute("/runs", {
    title: "Runs",
    sentence: "Active and finished runs come here next.",
    classic: { href: classicHref("#view=shipped"), label: "Open Finished in the classic console" },
  }),
  pageRoute("/tickets", Tickets),
  placeholderRoute("/findings", {
    title: "Findings",
    sentence: "Findings across runs come here. Until then, each run's findings are on its page in the classic console.",
    classic: { href: classicHref(), label: "Open the classic console" },
  }),
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
