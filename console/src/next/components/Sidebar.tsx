import { useState, type ReactNode } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { formatDuration } from "../../lib/format";
import { groupByHost, hostItems } from "../../lib/hosts";
import { applyTheme, readTheme, writeTheme } from "../../lib/theme";
import { scopedHosts, useConsolePeers } from "../scope";
import {
  ChevronIcon,
  FindingsIcon,
  Mark,
  MoonIcon,
  OverviewIcon,
  RunsIcon,
  SignalIcon,
  SunIcon,
  TicketsIcon,
} from "./icons";

export const RAIL_KEY = "holophyte.next.rail";

const PAGES = [
  { to: "/", label: "Overview", icon: <OverviewIcon /> },
  { to: "/runs", label: "Runs", icon: <RunsIcon /> },
  { to: "/tickets", label: "Tickets", icon: <TicketsIcon /> },
  { to: "/findings", label: "Findings", icon: <FindingsIcon /> },
] as const;

function readFolded(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === "folded";
  } catch {
    return false;
  }
}

function writeFolded(folded: boolean): void {
  try {
    if (folded) localStorage.setItem(RAIL_KEY, "folded");
    else localStorage.removeItem(RAIL_KEY);
  } catch {
    // Storage may be disabled; the fold still holds for this page.
  }
}

type Shown = "light" | "dark";

function shownTheme(): Shown {
  const stored = readTheme();
  if (stored !== "system") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

const THEME_NAMES: Record<Shown, string> = { light: "dawn", dark: "night" };

function ThemeToggle({ folded }: { folded: boolean }) {
  const [theme, setTheme] = useState<Shown>(shownTheme);
  const other: Shown = theme === "dark" ? "light" : "dark";
  const name = THEME_NAMES[theme];
  return (
    <button
      type="button"
      aria-label={`Colour theme: ${name}. Switch to ${THEME_NAMES[other]}`}
      title={folded ? `${name[0]!.toUpperCase()}${name.slice(1)}` : undefined}
      onClick={() => {
        writeTheme(other);
        applyTheme(other);
        setTheme(other);
      }}
      className="flex min-h-11 min-w-11 items-center justify-center gap-2 self-start border border-rule px-3 text-[12px] font-medium uppercase tracking-[0.12em] text-ink-2 hover:bg-paper-2 hover:text-ink"
    >
      {theme === "dark" ? <MoonIcon /> : <SunIcon />}
      {!folded && <span>{theme === "dark" ? "Night" : "Dawn"}</span>}
    </button>
  );
}

function PageLink({
  to,
  label,
  icon,
  badge,
  folded,
}: {
  to: (typeof PAGES)[number]["to"];
  label: string;
  icon: ReactNode;
  badge: number;
  folded: boolean;
}) {
  return (
    <Link
      to={to}
      search={(previous) => ({ project: previous.project })}
      activeOptions={{ exact: true, includeSearch: false }}
      aria-label={badge > 0 ? `${label}, ${badge} need you` : label}
      title={folded ? label : undefined}
      className={`relative flex min-h-11 items-center gap-3 border-l-2 border-transparent text-[12px] font-medium uppercase tracking-[0.12em] text-ink-2 hover:bg-paper-2 hover:text-ink data-[status=active]:border-act data-[status=active]:bg-act-wash data-[status=active]:text-act ${
        folded ? "justify-center px-0" : "px-4"
      }`}
    >
      <span className="relative">
        {icon}
        {folded && badge > 0 && (
          <span
            data-badge
            aria-hidden="true"
            className="absolute -top-2 -right-2.5 font-mono text-[9px] leading-none font-semibold text-need"
          >
            {badge}
          </span>
        )}
      </span>
      {!folded && <span className="flex-1">{label}</span>}
      {!folded && badge > 0 && (
        <span data-badge aria-hidden="true" className="font-mono text-[12px] font-semibold tracking-normal text-need">
          {badge}
        </span>
      )}
    </Link>
  );
}

function pollLine(hostLabels: string[], polledAgo: number | null): string {
  if (polledAgo == null) return "polling…";
  const host = hostLabels.length === 1 ? hostLabels[0] : `${hostLabels.length} hosts`;
  return `${host} · polled ${formatDuration(polledAgo)} ago`;
}

export function Sidebar() {
  const { hosts, polledAgo, now } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const [folded, setFolded] = useState(readFolded);
  const needs = scopedHosts(hosts, project).reduce((total, host) => total + hostItems(host, now).length, 0);
  const failing = hosts.some((host) => host.error != null);
  const polled = pollLine(groupByHost(hosts).map((group) => group.label), polledAgo);

  const fold = () => {
    setFolded(!folded);
    writeFolded(!folded);
  };

  return (
    <aside
      aria-label="Console"
      data-folded={folded}
      className={`flex w-full shrink-0 flex-col border-b border-rule bg-canvas min-[720px]:h-screen min-[720px]:overflow-y-auto min-[720px]:border-r min-[720px]:border-b-0 ${
        folded ? "min-[720px]:w-[56px]" : "min-[720px]:w-[232px]"
      }`}
    >
      <div className={`flex h-[60px] shrink-0 items-center border-b border-rule ${folded ? "justify-center" : "justify-between pl-4"}`}>
        {!folded && (
          <Link
            to="/"
            search={(previous) => ({ project: previous.project })}
            className="flex items-center gap-2 text-act"
          >
            <Mark />
            <span className="text-[16px] font-semibold tracking-[0.02em] text-ink">holophyte</span>
          </Link>
        )}
        <button
          type="button"
          aria-label={folded ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!folded}
          onClick={fold}
          className="flex size-11 items-center justify-center text-ink-3 hover:text-ink"
        >
          <ChevronIcon folded={folded} />
        </button>
      </div>

      <div data-slot="project-switcher" />

      <nav aria-label="Pages" className="flex flex-col py-3">
        {PAGES.map((page) => (
          <PageLink key={page.to} {...page} badge={page.to === "/" ? needs : 0} folded={folded} />
        ))}
      </nav>

      <footer className={`mt-auto flex flex-col gap-2 border-t border-rule py-3 ${folded ? "items-center px-1.5" : "px-3"}`}>
        <p className="flex min-w-0 items-center gap-1 font-mono text-[11px] tracking-[-0.02em] whitespace-nowrap text-ink-3" title={folded ? polled : undefined}>
          <SignalIcon className={failing ? "text-bad" : "text-live"} />
          {!folded && <span className="truncate">{polled}</span>}
        </p>
        {!folded && (
          <a href="/" className="text-[12px] text-ink-2 underline-offset-2 hover:text-ink hover:underline">
            Classic console
          </a>
        )}
        <ThemeToggle folded={folded} />
      </footer>
    </aside>
  );
}
