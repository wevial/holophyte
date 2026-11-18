import { createContext, Fragment, useContext, useRef, useState, type ReactNode, type Ref } from "react";
import { flushSync } from "react-dom";
import { Link, useNavigate } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { projectName } from "../../lib/derive";
import { formatClock, formatSettled } from "../../lib/format";
import { scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { SCOUT_DOTS } from "../scout";
import {
  cappedReason,
  keepList,
  LISTED_KINDS,
  SCOUT_STATES,
  scoutGroups,
  scoutKey,
  scoutOutcome,
  scoutState,
  shownScouts,
  useScouts,
  type ScoutGroup,
  type ScoutListRow,
  type ScoutOutcome,
  type ScoutsSearch,
} from "../scouts";
import { useScoutListWidth } from "../viewport";
import { Btn } from "./Btn";
import { FactIcon, PanelLeftIcon } from "./icons";
import { FilterSelect, SearchField } from "./ListFilters";
import type { LaunchProject } from "./ScoutLaunch";
import { StateDot } from "./StateDot";

const DASH = "–";
const LIST_ID = "scout-list";
const HIDDEN_KEY = "holophyte.next.scout-list";

const capital = (word: string) => word[0]!.toUpperCase() + word.slice(1);
const STATE_OPTIONS = SCOUT_STATES.map((state) => ({ value: state, label: capital(state) }));
const KIND_OPTIONS = LISTED_KINDS.map((kind) => ({ value: kind, label: capital(kind) }));

export function ScoutFilters({ searchWidth }: { searchWidth?: string }) {
  return (
    <>
      <SearchField label="Search scouts" width={searchWidth} />
      <FilterSelect label="State" param="filter" allLabel="All states" options={STATE_OPTIONS} />
      <FilterSelect label="Kind" param="type" allLabel="All kinds" options={KIND_OPTIONS} />
    </>
  );
}

export function ScoutGroups({ groups, row }: { groups: ScoutGroup[]; row: (row: ScoutListRow) => ReactNode }) {
  return (
    <ol aria-label="Scouts, newest first" className="m-0 flex list-none flex-col gap-5 p-0">
      {groups.map((group) => (
        <li key={`${group.base} ${group.project}`} data-scout-group>
          <div className="flex h-[30px] items-center gap-2.5 border-b border-hair px-2.5">
            <h2 className="font-sans text-[13px] font-semibold text-ink">{group.project}</h2>
            <span data-count className="font-mono text-[12px] text-ink-3">{group.rows.length}</span>
          </div>
          <ol aria-label={`${group.project} scouts`} className="m-0 list-none p-0">
            {group.rows.map(row)}
          </ol>
        </li>
      ))}
    </ol>
  );
}

export const EMPTY_LINE = "m-0 flex items-center gap-2 px-2.5 py-2.5 font-sans text-[13px] leading-[1.4] text-ink-3";
export const NEW_SCOUT = "h-7! gap-1.5! rounded-[7px]! px-2.5! text-[12.5px]!";
export const PLUS = (
  <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true">
    <path d="M8 3.5v9M3.5 8h9" />
  </svg>
);

/** The projects New scout may launch on in the scope, and the scoped hosts. */
export function useLaunchProjects(project: string | undefined) {
  const { hosts } = useConsolePeers();
  const scoped = scopedHosts(hosts, project);
  const projects: LaunchProject[] = scoped.flatMap((host) =>
    host.status?.actions === true ? [{ base: host.base, name: projectName(host.status.project) }] : [],
  );
  return { scoped, projects };
}

function Outcome({ outcome }: { outcome: ScoutOutcome }) {
  if (outcome.kind === "failed") return <span title={outcome.text} className="font-semibold text-bad">{cappedReason(outcome.text)}</span>;
  if (outcome.kind === "back") return <>{outcome.text}</>;
  return outcome.parts.map((part, index) => (
    <Fragment key={part.words}>
      {index > 0 && " · "}
      <span data-to-file={part.toFile ? "" : undefined} className={part.toFile ? "font-semibold text-need" : undefined}>
        {part.count} {part.words}
      </span>
    </Fragment>
  ));
}

const dayTime = (at: number) => `${new Date(at).toLocaleDateString("en-US", { weekday: "short" })} ${formatClock(at)}`;

function ScoutListItem({ row, current }: { row: ScoutListRow; current: boolean }) {
  const state = scoutState(row.phase);
  const took = state === "queued" || row.elapsed_ms == null ? DASH : formatSettled(row.elapsed_ms);
  const outcome = scoutOutcome(row);
  const empty = outcome.kind === "counts" ? outcome.parts.length === 0 : outcome.text === "";
  return (
    <li data-row data-scout={row.id} aria-current={current ? "page" : undefined} className="border-b border-rule-2 py-px last:border-b-0">
      <Link
        to="/scouts/$project/$scout"
        params={{ project: row.project, scout: String(row.id) }}
        search={(previous: ScoutsSearch & ScopeSearch) => keepList(previous)}
        className={`grid grid-cols-[16px_minmax(0,1fr)] gap-x-2.5 rounded-[8px] px-2.5 py-3 text-ink no-underline transition-colors duration-(--motion-colour) ${current ? "bg-row-selected" : "hover:bg-hover"}`}
      >
        <span className="mt-[2px] self-start"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
        <span className="flex min-w-0 flex-col gap-[5px]">
          <span data-cell="title" className="line-clamp-2 font-sans text-[14px] font-semibold leading-[1.3] text-ink">{row.title}</span>
          <span data-scout-row-facts className={`font-sans text-[12.5px] leading-[1.4] text-ink-3 ${outcome.kind === "failed" ? "truncate" : "[overflow-wrap:anywhere]"}`}>
            {row.kind}{" · "}{took}{" · "}{dayTime(row.started_ms ?? row.created_ms)}
            {!empty && <>{" · "}<Outcome outcome={outcome} /></>}
          </span>
        </span>
      </Link>
    </li>
  );
}

function readHidden(): boolean {
  try {
    return localStorage.getItem(HIDDEN_KEY) === "hidden";
  } catch {
    return false;
  }
}

function writeHidden(hidden: boolean): void {
  try {
    if (hidden) localStorage.setItem(HIDDEN_KEY, "hidden");
    else localStorage.removeItem(HIDDEN_KEY);
  } catch {
    // Storage may be disabled; the choice still holds for this page.
  }
}

const ShowListContext = createContext<{ show: () => void; ref: Ref<HTMLButtonElement> } | null>(null);

/** The button that brings a hidden scout list back, on a padded row of its own when `row`; nothing while the list shows. */
export function ShowScoutList({ row = false }: { row?: boolean }) {
  const list = useContext(ShowListContext);
  if (list == null) return null;
  const button = (
    <button ref={list.ref} type="button" aria-expanded={false} aria-controls={LIST_ID} onClick={list.show} className="btn shrink-0 gap-1.5" data-variant="quiet">
      <PanelLeftIcon />
      Scouts
    </button>
  );
  return row ? <div className="px-6 pt-5 max-[720px]:px-4">{button}</div> : button;
}

function NewScout({ project, current }: { project: string | undefined; current: string | null }) {
  const { projects } = useLaunchProjects(project);
  const navigate = useNavigate();
  const of = current == null ? undefined : current.slice(0, current.lastIndexOf("/"));
  const canLaunch = projects.length > 0;
  return (
    <Btn variant="primary" className={NEW_SCOUT} expanded={false} disabled={!canLaunch} title={canLaunch ? undefined : ACTIONS_OFF}
      onClick={() => void navigate({ to: "/scouts", search: (previous: ScoutsSearch & ScopeSearch) => ({ ...keepList(previous), new: of }) })}>
      {PLUS}
      New scout
    </Btn>
  );
}

/** The scout list beside `children` from the 1100px breakpoint, `current` (a scout key) marked;
 *  `newScout` replaces the button that opens New scout on the Scouts page. Below it, `children` alone,
 *  in the same tree so that crossing the breakpoint keeps their state. */
export function ScoutsBeside({ search, current, newScout, children }: {
  search: ScoutsSearch; current: string | null; newScout?: ReactNode; children: ReactNode;
}) {
  const wide = useScoutListWidth();
  const { project, rows: listed } = useScouts();
  const [hidden, setHidden] = useState(readHidden);
  const hideButton = useRef<HTMLButtonElement>(null);
  const showButton = useRef<HTMLButtonElement>(null);
  const toggle = (next: boolean) => {
    flushSync(() => setHidden(next));
    writeHidden(next);
    (next ? showButton : hideButton).current?.focus();
  };
  const rows = shownScouts(listed, search);
  const narrowed = search.q != null || search.filter != null || search.type != null;
  return (
    <div data-scouts-beside className="flex min-h-full items-stretch">
      {wide && !hidden && (
        <aside id={LIST_ID} aria-label="Scout list" data-scout-list
          className="sticky top-0 hidden h-(--panel-height) w-[330px] shrink-0 flex-col border-r border-rule bg-canvas min-[1100px]:flex">
          <div className="flex flex-col gap-3 border-b border-rule-2 px-4 pt-4 pb-3">
            <div className="flex items-center gap-2">
              <button ref={hideButton} type="button" aria-label="Hide scout list" title="Hide scout list" aria-expanded={true} aria-controls={LIST_ID}
                onClick={() => toggle(true)}
                className="inline-flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-[6px] text-ink-2 transition-colors duration-150 hover:bg-hover hover:text-ink aria-expanded:bg-selected aria-expanded:text-ink motion-reduce:transition-none">
                <PanelLeftIcon />
              </button>
              <h2 className="m-0 flex-1 font-sans text-[20px] font-semibold leading-[1.25] text-ink">Scouts</h2>
              {newScout ?? <NewScout project={project} current={current} />}
            </div>
            <div className="flex flex-wrap gap-2">
              <ScoutFilters searchWidth="w-full" />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 py-3.5">
            {rows.length === 0 ? (
              <p className={EMPTY_LINE}><FactIcon ok />{narrowed ? "No scouts match" : `No scouts in ${project ?? "any project"}.`}</p>
            ) : (
              <ScoutGroups groups={scoutGroups(rows)}
                row={(row) => <ScoutListItem key={`${row.base} ${row.id}`} row={row} current={scoutKey(row) === current} />} />
            )}
          </div>
        </aside>
      )}
      <ShowListContext.Provider value={wide && hidden ? { show: () => toggle(false), ref: showButton } : null}>
        <div data-scout-page className="min-h-full min-w-0 flex-1 bg-paper">{children}</div>
      </ShowListContext.Provider>
    </div>
  );
}
