import { createContext, Fragment, useContext, useRef, useState, type ReactNode, type Ref } from "react";
import { flushSync } from "react-dom";
import { Link, useNavigate } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { projectName } from "../../lib/derive";
import { formatClock, formatSettled } from "../../lib/format";
import { scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { SCOUT_DOTS } from "../scout";
import {
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
import { PanelLeftIcon } from "./icons";
import { LaneHead } from "./LaneHead";
import { FilterSelect, SearchField } from "./ListFilters";
import type { LaunchProject } from "./ScoutLaunch";
import { StateDot } from "./StateDot";

const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";
export const EMPTY_LINE = "m-0 border border-rule bg-paper px-3 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3";
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

export function ScoutGroups({ groups, row, boxed = true }: { groups: ScoutGroup[]; row: (row: ScoutListRow) => ReactNode; boxed?: boolean }) {
  return (
    <ol aria-label="Scouts, newest first" className="m-0 flex list-none flex-col gap-[22px] p-0">
      {groups.map((group) => (
        <li key={`${group.base} ${group.project}`} data-scout-group>
          <div className={boxed ? undefined : "px-4"}>
            <LaneHead name={group.project} held={false} meta={`${group.rows.length} ${group.rows.length === 1 ? "scout" : "scouts"}`} attached={boxed} />
          </div>
          <ol aria-label={`${group.project} scouts`} className={`m-0 list-none p-0 ${boxed ? "border border-rule bg-paper" : "mt-2 border-t border-rule-2"}`}>
            {group.rows.map(row)}
          </ol>
        </li>
      ))}
    </ol>
  );
}

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
  if (outcome.kind === "failed") return <span className="font-semibold text-bad">{outcome.text}</span>;
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
    <li data-row data-scout={row.id} aria-current={current ? "page" : undefined} className={`border-b border-rule-2 ${current ? "bg-row-selected shadow-[inset_2px_0_0_var(--act)]" : ""}`}>
      <Link
        to="/scouts/$project/$scout"
        params={{ project: row.project, scout: String(row.id) }}
        search={(previous: ScoutsSearch & ScopeSearch) => keepList(previous)}
        className={`grid grid-cols-[16px_minmax(0,1fr)] gap-x-2.5 px-4 py-3.5 text-ink no-underline transition-colors duration-(--motion-colour) ${current ? "" : "hover:bg-paper-2"}`}
      >
        <span className="mt-[2px] self-start"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
        <span className="flex min-w-0 flex-col gap-[5px]">
          <span data-cell="title" className="line-clamp-2 font-sans text-[14px] font-semibold leading-[1.3] text-ink">{row.title}</span>
          <span data-scout-row-facts className={`${MONO} [overflow-wrap:anywhere]`}>
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
const ListShownContext = createContext(false);

/** Whether the scout list is drawn beside the page, taking 330px of its width. */
export const useScoutListShown = () => useContext(ListShownContext);

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
    <Btn variant="primary" expanded={false} disabled={!canLaunch} title={canLaunch ? undefined : ACTIONS_OFF}
      onClick={() => void navigate({ to: "/scouts", search: (previous: ScoutsSearch & ScopeSearch) => ({ ...keepList(previous), new: of }) })}>
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
          className="sticky top-0 hidden h-screen w-[330px] shrink-0 flex-col border-r border-rule bg-canvas min-[1100px]:flex">
          <div className="flex flex-col gap-3 border-b border-rule-2 px-4 pt-4 pb-3">
            <div className="flex items-center gap-2">
              <button ref={hideButton} type="button" aria-label="Hide scout list" title="Hide scout list" aria-expanded={true} aria-controls={LIST_ID}
                onClick={() => toggle(true)} className="btn w-9 shrink-0 px-0" data-variant="quiet">
                <PanelLeftIcon />
              </button>
              <h2 className="m-0 flex-1 font-sans text-[20px] font-semibold leading-[1.25] text-ink">Scouts</h2>
              {newScout ?? <NewScout project={project} current={current} />}
            </div>
            <div className="flex flex-wrap gap-2">
              <ScoutFilters searchWidth="w-full" />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-0 py-3.5">
            {rows.length === 0 ? (
              <p className={`${EMPTY_LINE} mx-4`}>{narrowed ? "No scouts match" : `No scouts in ${project ?? "any project"}.`}</p>
            ) : (
              <ScoutGroups boxed={false} groups={scoutGroups(rows)}
                row={(row) => <ScoutListItem key={`${row.base} ${row.id}`} row={row} current={scoutKey(row) === current} />} />
            )}
          </div>
        </aside>
      )}
      <ShowListContext.Provider value={wide && hidden ? { show: () => toggle(false), ref: showButton } : null}>
        <ListShownContext.Provider value={wide && !hidden}>
          <div data-scout-page className="min-h-full min-w-0 flex-1 bg-paper">{children}</div>
        </ListShownContext.Provider>
      </ShowListContext.Provider>
    </div>
  );
}
