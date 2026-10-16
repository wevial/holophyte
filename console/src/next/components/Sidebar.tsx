import { useContext, useEffect, useState, type ReactNode } from "react";
import { Link, useRouterState, useSearch } from "@tanstack/react-router";
import { applyTheme, readTheme, writeTheme } from "../../lib/theme";
import { classicHref, writeView } from "../../lib/view";
import { useOpenCount } from "../findings";
import { footerLine } from "../notices";
import { switcherRows } from "../projects";
import { useBoardStories, useReadiness, type ListedStory } from "../queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { FILTER_LABELS, TICKET_FILTERS, ticketView, useTicketBoard } from "../tickets";
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
const STORIES_KEY = "holophyte.next.stories";

const PAGES = [
  { to: "/", label: "Overview", icon: <OverviewIcon />, noun: "need you" },
  { to: "/runs", label: "Runs", icon: <RunsIcon />, noun: "" },
  { to: "/tickets", label: "Tickets", icon: <TicketsIcon />, noun: "" },
  { to: "/findings", label: "Findings", icon: <FindingsIcon />, noun: "open" },
] as const;

function readFolded(key = RAIL_KEY): boolean {
  try {
    return localStorage.getItem(key) === "folded";
  } catch {
    return false;
  }
}

function writeFolded(folded: boolean, key = RAIL_KEY): void {
  try {
    if (folded) localStorage.setItem(key, "folded");
    else localStorage.removeItem(key);
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
  noun,
  badge,
  folded,
  active,
}: {
  to: (typeof PAGES)[number]["to"];
  label: string;
  icon: ReactNode;
  noun: string;
  badge: number;
  folded: boolean;
  active: boolean;
}) {
  return (
    <Link
      to={to}
      search={keepScope}
      activeOptions={{ exact: to === "/", includeSearch: false }}
      {...(active ? { "data-status": "active", "aria-current": "page" as const } : {})}
      aria-label={badge > 0 ? `${label}, ${badge} ${noun}` : label}
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

const GROUP_LABEL = "pt-2.5 pr-4 pb-1 pl-9 font-mono text-[11px] leading-[1.3] font-medium uppercase tracking-[0.1em] text-ink-3";

function ViewList() {
  const { cards } = useTicketBoard();
  return (
    <>
      <p className={GROUP_LABEL}>Views</p>
      <ul aria-label="Views" className="ml-5 flex flex-col border-l border-rule-2">
        {TICKET_FILTERS.map((filter) => (
          <li key={filter}>
            <Link
              to="/tickets"
              search={(previous: ScopeSearch) => ({ ...keepScope(previous), filter })}
              activeOptions={{ exact: true, includeSearch: true }}
              aria-label={FILTER_LABELS[filter]}
              className="flex min-h-9 items-center justify-between gap-2 px-3 font-sans text-[13px] text-ink-2 hover:bg-paper-2 hover:text-ink data-[status=active]:bg-act-wash data-[status=active]:text-act"
            >
              <span>{FILTER_LABELS[filter]}</span>
              <span data-count className="font-mono text-[11px] font-medium text-ink-3">
                {cards.filter((card) => ticketView(card) === filter).length}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}

function StoryList({ stories }: { stories: ListedStory[] }) {
  const [hidden, setHidden] = useState(() => readFolded(STORIES_KEY));
  const toggle = () => {
    setHidden(!hidden);
    writeFolded(!hidden, STORIES_KEY);
  };
  return (
    <div className="relative">
      <button
        type="button"
        aria-expanded={!hidden}
        aria-controls={hidden ? undefined : "sidebar-stories"}
        aria-label={hidden ? "Show the story list" : "Hide the story list"}
        onClick={toggle}
        className="absolute top-0 right-2 flex size-8 items-center justify-center text-ink-3 hover:text-ink"
      >
        <span className={hidden ? "rotate-180" : "-rotate-90"}>
          <ChevronIcon folded={false} />
        </span>
      </button>
      <p className={GROUP_LABEL}>Stories</p>
      {!hidden && (
        <ul id="sidebar-stories" aria-label="Stories" className="ml-5 flex flex-col border-l border-rule-2">
          {stories.map((story) => (
            <li key={`${story.project}/${story.ticket}`}>
              <Link
                to="/stories/$project/$key"
                params={{ project: story.project, key: story.ticket }}
                search={keepScope}
                activeOptions={{ exact: true, includeSearch: false }}
                aria-label={`${story.ticket} ${story.title}, ${story.merged} of ${story.total} merged`}
                className="flex min-h-11 flex-col justify-center px-3 text-ink-2 hover:bg-paper-2 hover:text-ink data-[status=active]:bg-act-wash data-[status=active]:text-act"
              >
                <span className="flex justify-between gap-2 font-mono text-[11px] leading-[1.3] font-medium text-ink-3">
                  <span>{story.ticket}</span>
                  <span>
                    {story.merged}/{story.total}
                  </span>
                </span>
                <span className="truncate font-sans text-[13px] leading-[1.3]">{story.title}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Sidebar() {
  const { hosts, now } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const [folded, setFolded] = useState(readFolded);
  const deps = useContext(PollDepsContext);
  const readiness = useReadiness(hosts, deps);
  const { stories } = useBoardStories(scopedHosts(hosts, project), deps);
  const onStory = useRouterState({ select: (state) => /^\/stories(\/|$)/.test(state.location.pathname) });
  const rows = switcherRows(hosts, now, readiness);
  const needs = rows.find((row) => row.name === (project ?? null))?.need ?? 0;
  const open = useOpenCount(scopedHosts(hosts, project), now);
  const badges: Partial<Record<(typeof PAGES)[number]["to"], number>> = { "/": needs, "/findings": open };
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
        {PAGES.map((page) =>
          page.to === "/tickets" ? (
            <div key={page.to}>
              <PageLink {...page} badge={badges[page.to] ?? 0} folded={folded} active={onStory} />
              {!folded && <ViewList />}
              {!folded && stories.length > 0 && <StoryList stories={stories} />}
            </div>
          ) : (
            <PageLink key={page.to} {...page} badge={badges[page.to] ?? 0} folded={folded} active={false} />
          ),
        )}
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
