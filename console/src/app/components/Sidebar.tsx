import { useContext, useEffect, useState, type ReactNode } from "react";
import { Link, useRouterState, useSearch } from "@tanstack/react-router";
import { useTheme } from "../appearance";
import type { Theme } from "../../lib/theme";
import { APP_DRAG, APP_NO_DRAG, inMacApp } from "../../lib/view";
import { useOpenCount } from "../findings";
import { footerLine } from "../notices";
import { hostsNeedingAttention } from "../settings";
import { switcherRows } from "../projects";
import { countScouts, useScouts } from "../scouts";
import { useBoardStories, useReadiness, type ListedStory } from "../queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { FILTER_LABELS, TICKET_FILTERS, ticketView, useTicketBoard } from "../tickets";
import {
  ChevronIcon,
  FindingsIcon,
  Mark,
  MoonIcon,
  OverviewIcon,
  PanelLeftIcon,
  RunsIcon,
  ScoutsIcon,
  SignalIcon,
  SettingsIcon,
  SunIcon,
  SystemIcon,
  TicketsIcon,
} from "./icons";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { plainTitle, TitleText } from "./TitleText";

const RAIL_KEY = "holophyte.next.rail";
const STORIES_KEY = "holophyte.next.stories";

const PAGES = [
  { to: "/", label: "Overview", icon: <OverviewIcon />, noun: "waiting on you" },
  { to: "/runs", label: "Runs", icon: <RunsIcon />, noun: "" },
  { to: "/tickets", label: "Tickets", icon: <TicketsIcon />, noun: "" },
  { to: "/findings", label: "Findings", icon: <FindingsIcon />, noun: "open" },
  { to: "/scouts", label: "Scouts", icon: <ScoutsIcon />, noun: "running" },
] as const;

type PageTo = (typeof PAGES)[number]["to"];

const BADGE_TONE: Partial<Record<PageTo, string>> = { "/scouts": "text-live" };

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

const FOOTER_BUTTON =
  "flex min-h-11 min-w-11 items-center justify-center gap-1.5 border border-rule bg-paper px-2 text-[11px] font-medium uppercase tracking-[0.1em] text-ink-2 hover:bg-paper-2 hover:text-ink";

const THEME_CHOICES: { theme: Theme; name: string; icon: ReactNode }[] = [
  { theme: "light", name: "Dawn", icon: <SunIcon /> },
  { theme: "dark", name: "Night", icon: <MoonIcon /> },
  { theme: "system", name: "System", icon: <SystemIcon /> },
];

function ThemeGroup({ folded }: { folded: boolean }) {
  const { theme, setTheme } = useTheme();
  return (
    <div
      role="group"
      aria-label="Theme"
      className={`flex shrink-0 border border-rule bg-paper ${folded ? "flex-col [&>*+*]:border-t" : "[&>*+*]:border-l"} [&>*+*]:border-rule`}
    >
      {THEME_CHOICES.map((choice) => (
        <button
          key={choice.theme}
          type="button"
          aria-label={choice.name}
          title={choice.name}
          aria-pressed={choice.theme === theme}
          onClick={() => setTheme(choice.theme)}
          className={`flex items-center justify-center hover:text-ink ${folded ? "h-7 w-11" : "min-h-11 w-7"} ${
            choice.theme === theme ? "bg-act-wash text-act" : "text-ink-3"
          }`}
        >
          {choice.icon}
        </button>
      ))}
    </div>
  );
}

function LinkBadge({ badge, folded, tone = "text-need" }: { badge: number; folded: boolean; tone?: string }) {
  if (badge <= 0) return null;
  return (
    <span data-badge aria-hidden="true" className={`font-mono font-semibold tracking-normal ${tone} ${folded ? "absolute -top-2 -right-2.5 text-[9px] leading-none" : "text-[12px]"}`}>
      {badge}
    </span>
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
  to: PageTo;
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
      className={`relative flex min-h-11 items-center gap-3 border-l-2 border-transparent text-[12px] font-medium uppercase tracking-[0.12em] text-ink-2 hover:bg-side-hover hover:text-ink data-[status=active]:border-act data-[status=active]:bg-act-wash data-[status=active]:text-act ${
        folded ? "justify-center px-0" : "px-4"
      }`}
    >
      <span className="relative">
        {icon}
        {folded && <LinkBadge badge={badge} folded tone={BADGE_TONE[to]} />}
      </span>
      {!folded && <span className="flex-1">{label}</span>}
      {!folded && <LinkBadge badge={badge} folded={false} tone={BADGE_TONE[to]} />}
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
              className="flex min-h-9 items-center justify-between gap-2 px-3 font-sans text-[13px] text-ink-2 hover:bg-side-hover hover:text-ink data-[status=active]:bg-act-wash data-[status=active]:text-act"
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
                aria-label={`${story.ticket} ${plainTitle(story.title)}, ${story.merged} of ${story.total} merged`}
                className="flex min-h-11 flex-col justify-center px-3 text-ink-2 hover:bg-side-hover hover:text-ink data-[status=active]:bg-act-wash data-[status=active]:text-act"
              >
                <span className="flex justify-between gap-2 font-mono text-[11px] leading-[1.3] font-medium text-ink-3">
                  <span>{story.ticket}</span>
                  <span>
                    {story.merged}/{story.total}
                  </span>
                </span>
                <span className="truncate font-sans text-[13px] leading-[1.3]"><TitleText>{story.title}</TitleText></span>
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
  const [macApp] = useState(inMacApp);
  const deps = useContext(PollDepsContext);
  const readiness = useReadiness(hosts, deps);
  const { stories } = useBoardStories(scopedHosts(hosts, project), deps);
  const onStory = useRouterState({ select: (state) => /^\/stories(\/|$)/.test(state.location.pathname) });
  const onSettings = useRouterState({ select: (state) => /^\/settings(\/|$)/.test(state.location.pathname) });
  const hostNeeds = hostsNeedingAttention(hosts);
  const rows = switcherRows(hosts, now, readiness);
  const needs = rows.find((row) => row.name === (project ?? null))?.need ?? 0;
  const open = useOpenCount(scopedHosts(hosts, project), now);
  const scouting = countScouts(useScouts().rows, "running");
  const badges: Partial<Record<PageTo, number>> = { "/": needs, "/findings": open, "/scouts": scouting };
  const failing = hosts.some((host) => host.error != null);
  const footer = footerLine(hosts, now);
  const polled = footer == null ? "polling…" : footer.tail == null ? footer.lead : `${footer.lead} · ${footer.tail.text}`;

  const settings = (
    <Link
      to="/settings/$tab"
      params={{ tab: "general" }}
      search={keepScope}
      aria-current={onSettings ? "page" : undefined}
      aria-label={hostNeeds > 0 ? `Settings, ${hostNeeds} ${hostNeeds === 1 ? "host needs" : "hosts need"} attention` : "Settings"}
      title={folded ? "Settings" : undefined}
      className={FOOTER_BUTTON}
    >
      <span className="relative">
        <SettingsIcon />
        {folded && <LinkBadge badge={hostNeeds} folded />}
      </span>
      {!folded && <span>Settings</span>}
      {!folded && <LinkBadge badge={hostNeeds} folded={false} />}
    </Link>
  );

  const fold = () => {
    setFolded(!folded);
    writeFolded(!folded);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "\\" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      event.preventDefault();
      fold();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  return (
    <aside
      aria-label="Console"
      data-folded={folded}
      className={`flex w-full shrink-0 flex-col border-r border-b border-rule bg-side min-[720px]:h-screen min-[720px]:overflow-y-auto min-[720px]:border-b-0 ${
        folded ? (macApp ? "min-[720px]:w-[112px]" : "min-[720px]:w-[56px]") : "min-[720px]:w-[232px]"
      }`}
    >
      <div
        className={`flex h-[52px] shrink-0 items-center gap-1 border-b border-rule-2 ${
          macApp ? `pl-[76px] ${APP_DRAG}` : folded ? "justify-center" : "pl-3"
        }`}
      >
        <button
          type="button"
          aria-label={folded ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!folded}
          onClick={fold}
          className={`flex size-7 items-center justify-center text-ink-3 hover:text-ink ${macApp ? APP_NO_DRAG : ""}`}
        >
          <PanelLeftIcon />
        </button>
        {!folded && (
          <Link
            to="/"
            search={keepScope}
            aria-label="holophyte"
            title="holophyte"
            className={`flex size-7 items-center justify-center text-act ${macApp ? APP_NO_DRAG : ""}`}
          >
            <Mark />
          </Link>
        )}
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
          <>
            {settings}
            <ThemeGroup folded />
          </>
        ) : (
          <div className="flex items-center justify-between">
            {settings}
            <ThemeGroup folded={false} />
          </div>
        )}
      </footer>
    </aside>
  );
}
