import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useRouterState, useSearch } from "@tanstack/react-router";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import { useTheme } from "../appearance";
import type { Theme } from "../../lib/theme";
import { APP_DRAG, APP_NO_DRAG, inMacApp } from "../../lib/view";
import { useOpenCount } from "../findings";
import { footerLine } from "../notices";
import { hostsNeedingAttention, SETTINGS_TABS } from "../settings";
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
  SignalIcon,
  SettingsIcon,
  SunIcon,
  SystemIcon,
  TicketsIcon,
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

const FOOTER_BUTTON =
  "flex min-h-11 min-w-11 items-center justify-center gap-1.5 rounded-[6px] px-2 text-[11px] font-medium uppercase tracking-[0.1em] text-ink-2 hover:bg-hover hover:text-ink";

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
      className={`flex shrink-0 overflow-hidden rounded-[6px] border border-rule-2 ${folded ? "flex-col [&>*+*]:border-t" : "[&>*+*]:border-l"} [&>*+*]:border-rule-2`}
    >
      {THEME_CHOICES.map((choice) => (
        <button
          key={choice.theme}
          type="button"
          aria-label={choice.name}
          title={choice.name}
          aria-pressed={choice.theme === theme}
          onClick={() => setTheme(choice.theme)}
          className={`flex items-center justify-center hover:text-ink focus-visible:-outline-offset-2! ${folded ? "h-7 w-11" : "min-h-11 w-7"} ${
            choice.theme === theme ? "bg-selected text-focus" : "text-ink-3 hover:bg-hover"
          }`}
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
            {...(active ? { "data-status": "active", "aria-current": "page" as const } : {})}
            aria-label={badge > 0 ? `${label}, ${badge} ${noun}` : label}
            aria-describedby={folded ? described : undefined}
            className={`group relative flex items-center rounded-[6px] text-ink-2 hover:bg-hover hover:text-ink data-[status=active]:bg-selected data-[status=active]:text-ink ${folded ? "mx-auto h-9 w-10 justify-center" : "mx-2 h-row gap-2.5 px-2.5 text-[13px] font-medium"}`}
          >
            <span className="relative">
              <span className="flex group-data-[status=active]:text-focus">{icon}</span>
              {folded && <LinkBadge badge={badge} folded tone={tone} />}
            </span>
            {!folded && <span className="flex-1">{label}</span>}
            {!folded && <LinkBadge badge={badge} folded={false} tone={tone ?? "text-ink-3"} />}
          </Link>
        )}
      />
      {folded && (
        <TooltipContent
          side="right"
          sideOffset={10}
          className="z-50 flex items-center gap-2 rounded-[6px] border border-rule-2 bg-raised px-2.5 py-1 text-[12px] font-medium whitespace-nowrap text-ink shadow-card"
        >
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
              className="group mr-2 flex min-h-9 items-center justify-between gap-2 rounded-[6px] px-3 font-sans text-[13px] text-ink-2 hover:bg-hover hover:text-ink data-[status=active]:bg-selected data-[status=active]:text-ink"
            >
              <span>{FILTER_LABELS[filter]}</span>
              <span data-count className="font-mono text-[11px] font-medium text-ink-3 group-data-[status=active]:text-ink-2">
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

export function Sidebar() {
  const { hosts, now } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const [storedFold, setFolded] = useState(readFolded);
  const [macApp] = useState(inMacApp);
  const deps = useContext(PollDepsContext);
  const readiness = useReadiness(hosts, deps);
  const { stories } = useBoardStories(scopedHosts(hosts, project), deps);
  const onStory = useRouterState({ select: (state) => /^\/stories(\/|$)/.test(state.location.pathname) });
  const onSettings = useRouterState({ select: (state) => /^\/settings(\/|$)/.test(state.location.pathname) });
  const settingsMenu = useRouterState({ select: (state) => SETTINGS_PATH.test(state.location.pathname) });
  const href = useRouterState({ select: (state) => state.location.href });
  const lastPage = useRef<string | null>(null);
  if (!settingsMenu) lastPage.current = href;
  const folded = storedFold && !settingsMenu;
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

  const toggleRef = useRef<HTMLButtonElement>(null);
  const refocusToggle = useRef(false);
  const fold = () => {
    if (settingsMenu) return;
    refocusToggle.current = toggleRef.current != null && document.activeElement === toggleRef.current;
    setFolded(!folded);
    writeFolded(!folded);
  };

  useEffect(() => {
    if (!refocusToggle.current) return;
    refocusToggle.current = false;
    toggleRef.current?.focus();
  }, [folded]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "\\" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      event.preventDefault();
      fold();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const toggle = (
    <button
      ref={toggleRef}
      type="button"
      aria-label={folded ? "Expand sidebar" : "Collapse sidebar"}
      aria-expanded={!folded}
      onClick={fold}
      className={`flex items-center justify-center rounded-[6px] text-ink-3 hover:bg-hover hover:text-ink ${folded ? "h-9 w-10" : "size-7"} ${macApp ? APP_NO_DRAG : ""}`}
    >
      <PanelLeftIcon />
    </button>
  );

  return (
    <aside
      aria-label="Console"
      data-folded={folded}
      className={`flex w-full shrink-0 flex-col border-r border-b border-rule-2 bg-side min-[720px]:h-screen min-[720px]:overflow-y-auto min-[720px]:border-b-0 ${
        folded ? (macApp ? "min-[720px]:w-[112px]" : "min-[720px]:w-[56px]") : "min-[720px]:w-[232px]"
      }`}
    >
      <div
        className={`flex h-[52px] shrink-0 items-center gap-1 ${
          macApp ? `pl-[76px] ${APP_DRAG}` : folded ? "justify-center" : "pl-4"
        } ${folded ? "" : "pr-2"}`}
      >
        <Link
          to="/"
          search={keepScope}
          aria-label="holophyte"
          title="holophyte"
          className={`flex min-h-7 items-center gap-2 text-[15px] font-semibold tracking-[-0.01em] text-ink ${folded ? "min-w-7 justify-center" : "mr-auto"} ${macApp ? APP_NO_DRAG : ""}`}
        >
          <Mark />
          {!folded && <span aria-hidden="true">holophyte</span>}
        </Link>
        {!folded && !settingsMenu && toggle}
      </div>

      {settingsMenu ? <SettingsNav back={lastPage.current} /> : <>
      <ProjectSwitcher rows={rows} folded={folded} />

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

      {folded && <div className="mt-auto flex justify-center pt-3">{toggle}</div>}

      <footer className={`flex flex-col gap-2 py-3 ${folded ? "items-center px-1.5" : "mt-auto px-3"}`}>
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
