import { useState } from "react";
import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { usePeers } from "../hooks/usePeers";
import { defaultPollDeps, type PollDeps } from "../lib/poll";
import { Sidebar } from "./components/Sidebar";
import { NotFound, Placeholder } from "./pages/Placeholder";
import { PeersContext, validateScope } from "./scope";

function Shell() {
  return (
    <div className="flex min-h-screen flex-col bg-canvas text-ink min-[720px]:h-screen min-[720px]:flex-row min-[720px]:overflow-hidden">
      <Sidebar />
      <main className="min-w-0 flex-1 min-[720px]:overflow-y-auto">
        <Outlet />
      </main>
    </div>
  );
}

const rootRoute = createRootRoute({
  validateSearch: validateScope,
  component: Shell,
  notFoundComponent: NotFound,
});

function placeholderRoute<TPath extends string>(path: TPath, page: Parameters<typeof Placeholder>[0]) {
  return createRoute({ getParentRoute: () => rootRoute, path, component: () => <Placeholder {...page} /> });
}

const routeTree = rootRoute.addChildren([
  placeholderRoute("/", {
    title: "Overview",
    sentence: "What needs you, what is running, pull requests and today's merges come here first.",
    classic: { href: "/", label: "Open Now in the classic console" },
  }),
  placeholderRoute("/runs", {
    title: "Runs",
    sentence: "Active and finished runs come here next.",
    classic: { href: "/#view=shipped", label: "Open Finished in the classic console" },
  }),
  placeholderRoute("/tickets", {
    title: "Tickets",
    sentence: "The board and the ticket list come here.",
    classic: { href: "/#view=board", label: "Open the Board in the classic console" },
  }),
  placeholderRoute("/findings", {
    title: "Findings",
    sentence: "Findings across runs come here. Until then, each run's findings are on its page in the classic console.",
    classic: { href: "/", label: "Open the classic console" },
  }),
]);

function createConsoleRouter() {
  return createRouter({ routeTree, history: createHashHistory() });
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
  const peers = usePeers(base, pollDeps);
  const [router] = useState(createConsoleRouter);
  return (
    <PeersContext.Provider value={peers}>
      <RouterProvider router={router} />
    </PeersContext.Provider>
  );
}
