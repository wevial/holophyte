import { useContext, useEffect, useState, type ReactNode } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { applyTheme, readTheme, writeTheme } from "../../lib/theme";
import { classicHref, writeView } from "../../lib/view";
import { footerLine } from "../notices";
import { switcherRows } from "../projects";
import { useReadiness } from "../queries";
import { keepScope, PollDepsContext, useConsolePeers } from "../scope";
import {
  ChevronIcon,
  ClassicIcon,
  FindingsIcon,
  Mark,
  MoonIcon,
  OverviewIcon,
  RunsIcon,
  SignalIcon,
  TicketsIcon,
} from "./icons";
import { ProjectSwitcher } from "./ProjectSwitcher";

const RAIL_KEY = "holophyte.next.rail";

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

const DARK_QUERY = "(prefers-color-scheme: dark)";

function systemDark(): boolean {
  return window.matchMedia?.(DARK_QUERY).matches ?? false;
}

const THEME_NAMES: Record<Shown, string> = { light: "Dawn", dark: "Night" };

function ThemeToggle({ folded }: { folded: boolean }) {
  const [stored, setStored] = useState(readTheme);
  const [dark, setDark] = useState(systemDark);
  useEffect(() => {
    if (stored !== "system") return;
    const query = window.matchMedia?.(DARK_QUERY);
    if (!query) return;
    const follow = () => setDark(query.matches);
    follow();
    query.addEventListener("change", follow);
    return () => query.removeEventListener("change", follow);
  }, [stored]);
  const theme: Shown = stored === "system" ? (dark ? "dark" : "light") : stored;
  const other: Shown = theme === "dark" ? "light" : "dark";
  const name = THEME_NAMES[theme];
  return (
    <button
      type="button"
      aria-label={`Colour theme: ${name.toLowerCase()}. Switch to ${THEME_NAMES[other].toLowerCase()}`}
      title={folded ? name : undefined}
      onClick={() => {
        writeTheme(other);
        applyTheme(other);
        setStored(other);
      }}
      className="flex min-h-11 min-w-11 items-center justify-center gap-2 self-start border border-rule bg-paper px-3 text-[11px] font-medium uppercase tracking-[0.1em] text-ink-2 hover:bg-paper-2 hover:text-ink"
    >
      <MoonIcon />
      {!folded && <span>{name}</span>}
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
      search={keepScope}
      activeOptions={{ exact: to === "/", includeSearch: false }}
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

export function Sidebar() {
  const { hosts, now } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const [folded, setFolded] = useState(readFolded);
  const readiness = useReadiness(hosts, useContext(PollDepsContext));
  const rows = switcherRows(hosts, now, readiness);
  const needs = rows.find((row) => row.name === (project ?? null))?.need ?? 0;
  const failing = hosts.some((host) => host.error != null);
  const footer = footerLine(hosts, now);
  const polled = footer == null ? "polling…" : footer.tail == null ? footer.lead : `${footer.lead} · ${footer.tail.text}`;

  const fold = () => {
    setFolded(!folded);
    writeFolded(!folded);
  };

  return (
    <aside
      aria-label="Console"
      data-folded={folded}
      className={`flex w-full shrink-0 flex-col border-b border-rule-2 bg-canvas min-[720px]:h-screen min-[720px]:overflow-y-auto min-[720px]:border-r min-[720px]:border-b-0 ${
        folded ? "min-[720px]:w-[56px]" : "min-[720px]:w-[232px]"
      }`}
    >
      <div
        className={`flex shrink-0 items-center border-b border-rule-2 ${
          folded ? "flex-col justify-center py-2" : "h-[60px] justify-between pl-4"
        }`}
      >
        <Link
          to="/"
          search={keepScope}
          aria-label={folded ? "holophyte" : undefined}
          title={folded ? "holophyte" : undefined}
          className={`flex items-center gap-2 text-act ${folded ? "size-11 justify-center" : ""}`}
        >
          <Mark />
          {!folded && <span className="text-[16px] font-semibold tracking-[0.02em] text-ink">holophyte</span>}
        </Link>
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

      <ProjectSwitcher rows={rows} folded={folded} />

      <nav aria-label="Pages" className="flex flex-col pt-2 pb-3">
        {PAGES.map((page) => (
          <PageLink key={page.to} {...page} badge={page.to === "/" ? needs : 0} folded={folded} />
        ))}
      </nav>

      <footer className={`mt-auto flex flex-col gap-2 border-t border-rule-2 py-3 ${folded ? "items-center px-1.5" : "px-3"}`}>
        <p className="flex min-w-0 items-start gap-1 font-mono text-[11px] leading-[1.4] tracking-[-0.02em] text-ink-3" title={folded ? polled : undefined}>
          <SignalIcon className={failing ? "text-bad" : "text-live"} />
          {!folded && (
            <span data-footer-line className="min-w-0 break-words">
              {footer == null ? polled : footer.lead}
              {footer?.tail && (
                <>
                  {" · "}
                  <span className={footer.tail.bad ? "text-bad" : undefined}>{footer.tail.text}</span>
                </>
              )}
            </span>
          )}
        </p>
        {folded ? (
          <a href={classicHref()} onClick={() => writeView("classic")} aria-label="Classic console" title="Classic console" className="flex size-11 items-center justify-center text-ink-2 hover:text-ink">
            <ClassicIcon />
          </a>
        ) : (
          <a href={classicHref()} onClick={() => writeView("classic")} className="text-[12px] text-ink-2 underline-offset-2 hover:text-ink hover:underline">
            Classic console
          </a>
        )}
        <ThemeToggle folded={folded} />
      </footer>
    </aside>
  );
}
