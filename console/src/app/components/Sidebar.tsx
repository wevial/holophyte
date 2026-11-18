import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useRouter, useRouterState, useSearch } from "@tanstack/react-router";
import { Tooltip, TooltipContent, TooltipShortcut, TooltipTrigger } from "@ui-sdk/react/tooltip";
import { useTheme } from "../appearance";
import type { Theme } from "../../lib/theme";
import { SWEEP_OK } from "../../lib/hosts";
import { APP_DRAG, APP_NO_DRAG, inMacApp } from "../../lib/view";
import { useOpenCount } from "../findings";
import { footerLine, type FooterLine } from "../notices";
import { hostsNeedingAttention, SETTINGS_TABS } from "../settings";
import { projectColours } from "../dayLog";
import { switcherRows } from "../projects";
import { countScouts, useScouts } from "../scouts";
import { useBoardStories, useMergeQueues, useReadiness, type ListedStory } from "../queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { FILTER_LABELS, TICKET_FILTERS, ticketView, useTicketBoard } from "../tickets";
import {
  ChevronIcon,
  FindingsIcon,
  FlowIcon,
  Mark,
  MergeQueueIcon,
  MoonIcon,
  OverviewIcon,
  PanelLeftIcon,
  RunsIcon,
  ScoutsIcon,
  SettingsIcon,
  SunIcon,
  SystemIcon,
  TicketsIcon,
  TurnArrow,
} from "./icons";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { SettingsNav } from "./SettingsNav";
import { plainTitle, TitleText } from "./TitleText";

const RAIL_KEY = "holophyte.next.rail";
const STORIES_KEY = "holophyte.next.stories";

const PAGES = [
  { to: "/", label: "Overview", icon: <OverviewIcon />, noun: "waiting on you" },
  { to: "/runs", label: "Runs", icon: <RunsIcon />, noun: "" },
  { to: "/merge-queue", label: "Merge queue", icon: <MergeQueueIcon />, noun: "queued" },
  { to: "/tickets", label: "Tickets", icon: <TicketsIcon />, noun: "" },
  { to: "/findings", label: "Findings", icon: <FindingsIcon />, noun: "open" },
  { to: "/scouts", label: "Scouts", icon: <ScoutsIcon />, noun: "running" },
  { to: "/flow", label: "Flow", icon: <FlowIcon />, noun: "" },
] as const;

const SETTINGS_PATH = new RegExp(`^/settings(/(${SETTINGS_TABS.map((tab) => tab.id).join("|")}))?/?$`);

type PageTo = (typeof PAGES)[number]["to"];

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

const FOOT_ICON = "relative flex size-7 shrink-0 items-center justify-center rounded-[6px]";

const TIP = "z-50 flex items-center gap-2 rounded-[6px] border border-rule-2 bg-raised px-2.5 py-1 text-[12px] font-medium whitespace-nowrap text-ink shadow-card";

type Connection = "live" | "stale" | "down";

const CONNECTION_FILL: Record<Connection, string> = { live: "fill-live", stale: "fill-ask", down: "fill-bad" };

function connectionOf(hosts: Parameters<typeof footerLine>[0], footer: FooterLine | null): Connection {
  if (hosts.length > 0 && hosts.every((host) => host.error != null)) return "down";
  if (footer == null) return "stale";
  const fine = (host: (typeof hosts)[number]) =>
    host.seen_ms != null && host.error == null && (host.host_status == null || SWEEP_OK.has(host.host_status.sweep.state));
  return hosts.every(fine) ? "live" : "stale";
}

function FootTip({ tip, folded, children }: { tip: string; folded: boolean; children: (trigger: Record<string, unknown>) => ReactNode }) {
  return (
    <Tooltip delay={300}>
      <TooltipTrigger render={({ type: _button, "aria-describedby": _same, ...trigger }) => <>{children(trigger)}</>} />
      <TooltipContent side={folded ? "right" : "top"} sideOffset={folded ? 10 : 8} className={TIP}>
        {tip}
      </TooltipContent>
    </Tooltip>
  );
}

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
      className={`flex shrink-0 gap-0.5 ${folded ? "flex-col" : ""}`}
    >
      {THEME_CHOICES.map((choice) => (
        <button
          key={choice.theme}
          type="button"
          aria-label={choice.name}
          title={choice.name}
          aria-pressed={choice.theme === theme}
          onClick={() => setTheme(choice.theme)}
          className={`${FOOT_ICON} ${choice.theme === theme ? "bg-selected text-ink" : "text-ink-3 hover:bg-hover hover:text-ink"}`}
        >
          {choice.icon}
        </button>
      ))}
    </div>
  );
}

const RAIL_BADGE: Record<string, string> = { "text-need": "bg-need text-raised", "text-live": "bg-live text-raised" };

function LinkBadge({ badge, folded, tone = "text-need" }: { badge: number; folded: boolean; tone?: string }) {
  if (badge <= 0) return null;
  if (folded) {
    return (
      <span data-badge aria-hidden="true" className={`absolute -top-2 -right-2.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-1 font-mono text-[9px] leading-none font-semibold ${RAIL_BADGE[tone] ?? "bg-selected text-ink"}`}>
        {badge}
      </span>
    );
  }
  return (
    <span data-badge aria-hidden="true" className={`font-mono text-[12px] font-medium tracking-normal ${tone} group-data-[status=active]:text-ink`}>
      {badge}
    </span>
  );
}

const NAV_ON = "bg-(image:--nav-on) text-ink";

function PageLink({
  to,
  label,
  icon,
  noun,
  badge,
  tone,
  folded,
  active,
}: {
  to: PageTo;
  label: string;
  icon: ReactNode;
  noun: string;
  badge: number;
  tone?: string;
  folded: boolean;
  active: boolean;
}) {
  return (
    <Tooltip delay={300} disabled={!folded}>
      <TooltipTrigger
        render={({ type: _button, "aria-describedby": described, ...trigger }) => (
          <Link
            {...trigger}
            to={to}
            search={keepScope}
            activeOptions={{ exact: to === "/", includeSearch: false }}
            activeProps={{ className: NAV_ON }}
            {...(active ? { "data-status": "active", "aria-current": "page" as const } : {})}
            aria-label={badge > 0 ? `${label}, ${badge} ${noun}` : label}
            aria-describedby={folded ? described : undefined}
            className={`group relative flex items-center rounded-[6px] text-ink-2 hover:bg-hover hover:text-ink ${active ? NAV_ON : ""} ${folded ? "mx-auto h-9 w-10 justify-center" : "mx-2 h-row gap-2.5 px-2.5 text-[13px] font-medium"}`}
          >
            {({ isActive }) => (
              <>
                <span className="relative">
                  <span className={`flex ${isActive || active ? "text-focus" : ""}`}>{icon}</span>
                  {folded && <LinkBadge badge={badge} folded tone={tone} />}
                </span>
                {!folded && <span className="flex-1">{label}</span>}
                {!folded && <LinkBadge badge={badge} folded={false} tone={tone ?? "text-ink-3"} />}
              </>
            )}
          </Link>
        )}
      />
      {folded && (
        <TooltipContent side="right" sideOffset={10} className={TIP}>
          {label}
          {badge > 0 && <span className="font-mono text-ink-3"> {badge}</span>}
        </TooltipContent>
      )}
    </Tooltip>
  );
}

const GROUP_LABEL = "pt-2.5 pr-4 pb-1 pl-9 font-mono text-[11px] leading-[1.3] font-medium uppercase tracking-[0.1em] text-ink-3";

function ViewList() {
  const { cards } = useTicketBoard();
  return (
    <ul aria-label="Ticket views" className="flex flex-col">
      {TICKET_FILTERS.map((filter) => (
        <li key={filter}>
          <Link
            to="/tickets"
            search={(previous: ScopeSearch) => ({ ...keepScope(previous), filter })}
            activeOptions={{ exact: true, includeSearch: true }}
            activeProps={{ className: NAV_ON }}
            aria-label={FILTER_LABELS[filter]}
            className="group mx-2 flex h-[27px] items-center justify-between gap-2 rounded-[6px] pr-2.5 pl-[34px] font-sans text-[13px] font-normal text-ink-2 hover:bg-hover hover:text-ink"
          >
            <span>{FILTER_LABELS[filter]}</span>
            <span data-count className="font-mono text-[12px] text-ink-3 group-data-[status=active]:text-ink-2">
              {cards.filter((card) => ticketView(card) === filter).length}
            </span>
          </Link>
        </li>
      ))}
    </ul>
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
                className="group mr-2 flex min-h-11 flex-col justify-center rounded-[6px] px-3 text-ink-2 hover:bg-hover hover:text-ink data-[status=active]:bg-selected data-[status=active]:text-ink"
              >
                <span className="flex justify-between gap-2 font-mono text-[11px] leading-[1.3] font-medium text-ink-3 group-data-[status=active]:text-ink-2">
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

function modChord(event: KeyboardEvent, key: string): boolean {
  return event.key === key && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey;
}

export type SidebarFold = { folded: boolean; foldable: boolean; fold: () => void };

export function useSidebarFold(): SidebarFold {
  const [storedFold, setFolded] = useState(readFolded);
  const settingsMenu = useRouterState({ select: (state) => SETTINGS_PATH.test(state.location.pathname) });
  const folded = storedFold && !settingsMenu;
  const fold = () => {
    if (settingsMenu) return;
    setFolded(!folded);
    writeFolded(!folded);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!modChord(event, "\\")) return;
      event.preventDefault();
      fold();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  return { folded, foldable: !settingsMenu, fold };
}

function useHistoryNav(bound: boolean) {
  const { history } = useRouter();
  const [reach, setReach] = useState(() => {
    const index = history.location.state.__TSR_index;
    return { index, furthest: index };
  });

  useEffect(
    () =>
      history.subscribe(({ location, action }) => {
        const index = location.state.__TSR_index;
        setReach((last) => ({ index, furthest: action.type === "PUSH" ? index : Math.max(last.furthest, index) }));
      }),
    [history],
  );

  const canBack = reach.index > 0;
  const canForward = reach.index < reach.furthest;
  const back = () => canBack && history.back();
  const forward = () => canForward && history.forward();

  useEffect(() => {
    if (!bound) return;
    const onKey = (event: KeyboardEvent) => {
      const go = modChord(event, "[") ? back : modChord(event, "]") ? forward : null;
      if (!go) return;
      event.preventDefault();
      go();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  return { canBack, canForward, back, forward };
}

const TITLE_BUTTON = "flex size-6 shrink-0 items-center justify-center rounded-[6px] text-ink-3 hover:bg-hover hover:text-ink";

function HistoryButton({ name, shortcut, enabled, go, next }: { name: string; shortcut: string; enabled: boolean; go: () => void; next?: boolean }) {
  return (
    <Tooltip delay={300} shortcut={shortcut}>
      <TooltipTrigger
        aria-label={name}
        disabled={!enabled}
        onClick={go}
        className={`${TITLE_BUTTON} ${APP_NO_DRAG} disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-ink-3`}
      >
        <TurnArrow next={next} />
      </TooltipTrigger>
      <TooltipContent side="bottom" align="start" sideOffset={6} className={TIP}>
        {name}
        <TooltipShortcut className="font-mono text-ink-3" />
      </TooltipContent>
    </Tooltip>
  );
}

export function TitleBar({ folded, foldable, fold }: SidebarFold) {
  const [macApp] = useState(inMacApp);
  const nav = useHistoryNav(macApp);
  const toggleName = folded ? "Expand sidebar" : "Collapse sidebar";
  return (
    <div data-title-bar="" className={`flex h-[30px] shrink-0 items-center gap-2 ${macApp ? `pl-[76px] ${APP_DRAG}` : "pl-3"}`}>
      {foldable && (
        <Tooltip delay={300} shortcut={"mod+\\"}>
          <TooltipTrigger
            aria-label={toggleName}
            aria-expanded={!folded}
            onClick={fold}
            className={`${TITLE_BUTTON} ${macApp ? APP_NO_DRAG : ""}`}
          >
            <PanelLeftIcon />
          </TooltipTrigger>
          <TooltipContent side="bottom" align="start" sideOffset={6} className={TIP}>
            {toggleName}
            <TooltipShortcut className="font-mono text-ink-3" />
          </TooltipContent>
        </Tooltip>
      )}
      <Link
        to="/"
        search={keepScope}
        aria-label="holophyte"
        title="holophyte"
        className={`flex h-6 items-center gap-1.5 text-[13px] font-semibold tracking-[-0.01em] text-ink [&_svg]:size-4 ${macApp ? APP_NO_DRAG : ""}`}
      >
        <Mark />
        <span aria-hidden="true" className="leading-none [text-box:trim-both_cap_alphabetic]">
          holophyte
        </span>
      </Link>
      {macApp && (
        <>
          <HistoryButton name="Back" shortcut="mod+[" enabled={nav.canBack} go={nav.back} />
          <HistoryButton name="Next" shortcut="mod+]" enabled={nav.canForward} go={nav.forward} next />
        </>
      )}
    </div>
  );
}

export function Sidebar({ folded }: { folded: boolean }) {
  const { hosts, now } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const deps = useContext(PollDepsContext);
  const readiness = useReadiness(hosts, deps);
  const { stories } = useBoardStories(scopedHosts(hosts, project), deps);
  const onStory = useRouterState({ select: (state) => /^\/stories(\/|$)/.test(state.location.pathname) });
  const onSettings = useRouterState({ select: (state) => /^\/settings(\/|$)/.test(state.location.pathname) });
  const settingsMenu = useRouterState({ select: (state) => SETTINGS_PATH.test(state.location.pathname) });
  const href = useRouterState({ select: (state) => state.location.href });
  const lastPage = useRef<string | null>(null);
  if (!settingsMenu) lastPage.current = href;
  const hostNeeds = hostsNeedingAttention(hosts);
  const rows = switcherRows(hosts, now, readiness);
  const needs = rows.find((row) => row.name === (project ?? null))?.need ?? 0;
  const open = useOpenCount(scopedHosts(hosts, project), now);
  const scouting = countScouts(useScouts().rows, "running");
  const queues = useMergeQueues(hosts, deps);
  const inScope = new Set(scopedHosts(hosts, project));
  const scopedQueues = queues.filter(({ host }) => inScope.has(host));
  const queued = scopedQueues.reduce((sum, { queue }) => sum + queue.entries.length, 0);
  const queueWaits = scopedQueues.some(({ queue }) => queue.left.some((row) => row.waits_on_you));
  const shown = PAGES.filter((page) => page.to !== "/merge-queue" || scopedQueues.some(({ queue }) => queue.configured === true));
  const badges: Partial<Record<PageTo, number>> = { "/": needs, "/merge-queue": queued, "/findings": open, "/scouts": scouting };
  const tones: Partial<Record<PageTo, string>> = { "/scouts": "text-live", "/merge-queue": queueWaits ? "text-need" : "text-live" };
  const footer = footerLine(hosts, now);
  const polled = footer == null ? "polling…" : footer.tail == null ? footer.lead : `${footer.lead} · ${footer.tail.text}`;
  const connection = connectionOf(hosts, footer);

  const dot = (
    <FootTip tip={polled} folded={folded}>
      {(trigger) => (
        <span
          {...trigger}
          tabIndex={0}
          role="img"
          aria-label={polled}
          data-connection={connection}
          className={`flex h-7 shrink-0 items-center justify-center rounded-[6px] ${folded ? "w-7" : "mr-auto w-5"}`}
        >
          <svg viewBox="0 0 16 16" width={7} height={7} aria-hidden="true" className="size-[7px]">
            <circle cx="8" cy="8" r="8" className={`${CONNECTION_FILL[connection]} ${connection === "live" ? "state-pulse" : ""}`} />
          </svg>
        </span>
      )}
    </FootTip>
  );

  const settings = (
    <FootTip tip="Settings" folded={folded}>
      {(trigger) => (
        <Link
          {...trigger}
          to="/settings/$tab"
          params={{ tab: "general" }}
          search={keepScope}
          aria-current={onSettings ? "page" : undefined}
          aria-label={hostNeeds > 0 ? `Settings, ${hostNeeds} ${hostNeeds === 1 ? "host needs" : "hosts need"} attention` : "Settings"}
          className={`${FOOT_ICON} text-ink-3 hover:bg-hover hover:text-ink`}
        >
          <SettingsIcon />
          <LinkBadge badge={hostNeeds} folded />
        </Link>
      )}
    </FootTip>
  );

  return (
    <aside
      aria-label="Console"
      data-folded={folded}
      className={`flex w-full shrink-0 flex-col border-b border-rule-2 min-[720px]:overflow-y-auto min-[720px]:border-b-0 ${
        folded ? "min-[720px]:w-[56px]" : "min-[720px]:w-[232px]"
      }`}
    >
      {settingsMenu ? <SettingsNav back={lastPage.current} /> : <>
      <ProjectSwitcher rows={rows} colours={projectColours(hosts)} folded={folded} />

      <nav aria-label="Pages" className="flex flex-col pt-2 pb-3">
        {shown.map((page) =>
          page.to === "/tickets" ? (
            <div key={page.to}>
              <PageLink {...page} badge={badges[page.to] ?? 0} tone={tones[page.to]} folded={folded} active={onStory} />
              {!folded && <ViewList />}
              {!folded && stories.length > 0 && <StoryList stories={stories} />}
            </div>
          ) : (
            <PageLink key={page.to} {...page} badge={badges[page.to] ?? 0} tone={tones[page.to]} folded={folded} active={false} />
          ),
        )}
      </nav>
      </>}

      <footer className={`mt-auto flex gap-0.5 py-3 ${folded ? "flex-col items-center px-1.5" : "items-center px-3"}`}>
        {dot}
        <ThemeGroup folded={folded} />
        {settings}
      </footer>
    </aside>
  );
}
