import { Fragment, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { projectName } from "../../lib/derive";
import { useAction } from "../actions";
import { formatSettled } from "../../lib/format";
import { Btn } from "../components/Btn";
import { LaneHead } from "../components/LaneHead";
import { FilterSelect, SearchField } from "../components/ListFilters";
import { EMPTY_DRAFT, prefilledDraft, ScoutLaunch, type LaunchProject } from "../components/ScoutLaunch";
import { dayClock } from "../components/ScoutFacts";
import { ScoutPane } from "../components/ScoutPane";
import { StateDot } from "../components/StateDot";
import { firstLine } from "../history";
import { keepScope, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { SCOUT_DOTS, scoutPage } from "../scout";
import {
  countScouts,
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
import { useWide } from "../viewport";

const GRID = "min-[720px]:grid min-[720px]:grid-cols-[16px_minmax(0,1fr)_76px_52px_88px_56px] min-[720px]:items-start min-[720px]:gap-3";
const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";
const EMPTY_LINE = "m-0 border border-rule bg-paper px-3 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3";

const capital = (word: string) => word[0]!.toUpperCase() + word.slice(1);
const STATE_OPTIONS = SCOUT_STATES.map((state) => ({ value: state, label: capital(state) }));
const KIND_OPTIONS = LISTED_KINDS.map((kind) => ({ value: kind, label: capital(kind) }));

/** The search a move within the Scouts page keeps: its scope and what narrows the list. */
const keepList = (previous: ScoutsSearch & ScopeSearch): ScoutsSearch & ScopeSearch => ({
  ...keepScope(previous), q: previous.q, filter: previous.filter, type: previous.type,
});

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

function helpers(row: ScoutListRow): string {
  const helper = row.helper ?? DASH;
  return row.tasks_total > 0 ? `${helper} ${row.tasks_done} of ${row.tasks_total}` : helper;
}

/** Under 720px a cell after the first in the fact line reads after a dot. */
const FACT = "[overflow-wrap:anywhere] max-[719px]:before:mx-1.5 max-[719px]:before:content-['·'] max-[719px]:first:before:content-none";

function ScoutTableRow({ row }: { row: ScoutListRow }) {
  const state = scoutState(row.phase);
  const quiet = state === "done" || state === "stopped" || state === "failed";
  const reason = state === "failed" ? firstLine(row.reason) : "";
  const took = state === "queued" || row.elapsed_ms == null ? DASH : formatSettled(row.elapsed_ms);
  return (
    <li data-row data-scout={row.id} className="border-b border-rule-2 last:border-b-0">
      <Link
        {...scoutPage(row.project, row.id)}
        className={`${GRID} grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-1 px-3 py-3 text-[14px] no-underline transition-colors duration-(--motion-colour) hover:bg-paper-2 ${quiet ? "text-ink-2" : "text-ink"}`}
      >
        <span data-cell="state" className="flex items-start pt-[3px]"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
        <span data-cell="title" className={`min-w-0 font-read text-[15px] font-medium leading-[1.35] min-[720px]:order-2 ${quiet ? "text-ink-2" : "text-ink"}`}>
          {row.title}
          {reason && <span data-reason className="ml-2 font-mono text-[12.5px] font-semibold text-bad">{reason}</span>}
        </span>
        <span className={`col-start-2 flex flex-wrap ${MONO} min-[720px]:contents`}>
          <span data-cell="kind" className={`${FACT} min-[720px]:order-3`}>
            <span className="min-[720px]:inline-block min-[720px]:align-top min-[720px]:leading-[15px] min-[720px]:border min-[720px]:border-rule min-[720px]:px-1 min-[720px]:text-[11px] min-[720px]:font-semibold min-[720px]:text-ink-2">{row.kind}</span>
          </span>
          <span data-cell="lead" className={`${FACT} min-[720px]:order-5`}>{row.lead ?? DASH}</span>
          <span data-cell="helpers" className={`${FACT} min-[720px]:order-6`}>{helpers(row)}</span>
          <span data-cell="took" className={`${FACT} whitespace-nowrap min-[720px]:order-7 min-[720px]:text-right`}>{took}</span>
        </span>
      </Link>
    </li>
  );
}

/** One group per project, each under its lane head over its rows. */
function ScoutGroups({ groups, row }: { groups: ScoutGroup[]; row: (row: ScoutListRow) => ReactNode }) {
  return (
    <ol aria-label="Scouts, newest first" className="m-0 flex list-none flex-col gap-[22px] p-0">
      {groups.map((group) => (
        <li key={`${group.base} ${group.project}`} data-scout-group>
          <LaneHead name={group.project} held={false} meta={`${group.rows.length} ${group.rows.length === 1 ? "scout" : "scouts"}`} />
          <ol aria-label={`${group.project} scouts`} className="m-0 list-none border border-rule bg-paper p-0">
            {group.rows.map(row)}
          </ol>
        </li>
      ))}
    </ol>
  );
}

function ScoutTable({ groups }: { groups: ScoutGroup[] }) {
  return (
    <div data-scout-table className="flex flex-col">
      <div aria-hidden="true" className={`${GRID} hidden px-[13px] pb-2.5 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3`}>
        <span />
        <span>Title</span>
        <span>Kind</span>
        <span>Lead</span>
        <span>Helpers</span>
        <span className="text-right">Took</span>
      </div>
      <ScoutGroups groups={groups} row={(row) => <ScoutTableRow key={`${row.base} ${row.id}`} row={row} />} />
    </div>
  );
}

function Outcome({ outcome }: { outcome: ScoutOutcome }) {
  if (outcome.kind === "failed") return <span className="font-semibold text-bad">{outcome.text}</span>;
  if (outcome.kind === "back") return <>{outcome.text}</>;
  return outcome.parts.map((part, index) => (
    <Fragment key={part.words}>
      {index > 0 && <span className="text-ink-3">{" · "}</span>}
      <span data-to-file={part.toFile ? "" : undefined} className={part.toFile ? "font-semibold text-need" : undefined}>
        <b className={`font-semibold ${part.toFile ? "" : "text-ink"}`}>{part.count}</b> {part.words}
      </span>
    </Fragment>
  ));
}

function ScoutListItem({ row, current }: { row: ScoutListRow; current: boolean }) {
  const state = scoutState(row.phase);
  const took = state === "queued" || row.elapsed_ms == null ? DASH : formatSettled(row.elapsed_ms);
  const outcome = scoutOutcome(row);
  const empty = outcome.kind === "counts" ? outcome.parts.length === 0 : outcome.text === "";
  return (
    <li data-row data-scout={row.id} aria-current={current ? "true" : undefined} className={`border-b border-rule-2 last:border-b-0 ${current ? "bg-row-selected shadow-[inset_2px_0_0_var(--act)]" : ""}`}>
      <Link
        to="/scouts"
        search={(previous: ScoutsSearch & ScopeSearch) => ({ ...keepList(previous), scout: scoutKey(row) })}
        className={`grid grid-cols-[16px_minmax(0,1fr)] gap-x-2.5 px-3.5 py-[13px] no-underline transition-colors duration-(--motion-colour) ${current ? "" : "hover:bg-paper-2"}`}
      >
        <span className="mt-[15px] self-start"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
        <span className="flex min-w-0 flex-col gap-1.5">
          <span data-cell="title" className="truncate font-read text-[15px] font-medium leading-[1.35] text-ink">{row.title}</span>
          <span data-scout-row-facts className={`${MONO} [overflow-wrap:anywhere]`}>
            <span className="inline-block border border-rule px-1 align-top text-[11px] font-semibold leading-[15px] text-ink-2">{row.kind}</span>
            {" · "}{took}{" · "}{dayClock(row.started_ms ?? row.created_ms)}
          </span>
          {!empty && <span data-scout-outcome className={`${MONO} text-ink-2 [overflow-wrap:anywhere]`}><Outcome outcome={outcome} /></span>}
        </span>
      </Link>
    </li>
  );
}

function ScoutList({ groups, empty, current }: { groups: ScoutGroup[]; empty: ReactNode; current: string | null }) {
  return (
    <div data-scout-list className="max-h-full w-[min(624px,45%)] shrink-0 self-start overflow-y-auto">
      {empty ?? <ScoutGroups groups={groups} row={(row) => <ScoutListItem key={`${row.base} ${row.id}`} row={row} current={scoutKey(row) === current} />} />}
    </div>
  );
}

/** The pane's scout: the `scout` search's, else the first shown row. */
function selection(search: ScoutsSearch, rows: ScoutListRow[]): { project: string; id: number } | null {
  if (search.scout != null) {
    const split = search.scout.lastIndexOf("/");
    return { project: search.scout.slice(0, split), id: Number(search.scout.slice(split + 1)) };
  }
  const first = rows[0];
  return first == null ? null : { project: first.project, id: first.id };
}

export function Scouts() {
  const { project, rows: listed } = useScouts();
  const { hosts } = useConsolePeers();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const launch = useAction();
  const scoped = scopedHosts(hosts, project);
  const projects: LaunchProject[] = scoped.flatMap((host) =>
    host.status?.actions === true ? [{ base: host.base, name: projectName(host.status.project) }] : [],
  );
  const canLaunch = projects.length > 0;
  const search = useSearch({ from: "/scouts" });
  const rows = shownScouts(listed, search);
  const groups = scoutGroups(rows);
  const navigate = useNavigate();
  const asked = search.new == null ? null : JSON.stringify([search.new, search.about, search.kind, search.ask]);
  const [filled, setFilled] = useState<string | null>(null);
  const fillable = projects.some((each) => each.name === search.new) || scoped.every((host) => host.status != null || host.error != null || host.needs_token);
  if (asked !== filled && (asked == null || (canLaunch && fillable))) {
    setFilled(asked);
    if (asked != null) {
      setDraft(prefilledDraft(search, projects));
      setOpen(true);
    }
  }
  const wide = useWide();
  const close = () => {
    setOpen(false);
    if (asked != null) void navigate({ to: "/scouts", search: (previous: ScoutsSearch & ScopeSearch) => ({ ...keepList(previous), scout: previous.scout }), replace: true });
    if (draft.about != null) setDraft({ ...draft, about: null });
  };
  const launching = open && canLaunch;
  const form = (pane: boolean) => (
    <ScoutLaunch
      key={filled ?? ""}
      projects={projects}
      action={launch}
      draft={draft}
      onDraft={setDraft}
      onCancel={close}
      onLaunched={() => {
        close();
        setDraft(EMPTY_DRAFT);
      }}
      pane={pane}
    />
  );
  const narrowed = search.q != null || search.filter != null || search.type != null;
  const empty = rows.length > 0 ? null : <p className={EMPTY_LINE}>{narrowed ? "No scouts match" : `No scouts in ${project ?? "any project"}.`}</p>;
  const shown = selection(search, rows);
  return (
    <section aria-labelledby="page-title" className={`flex flex-col gap-6 px-6 pt-3.5 pb-7 max-[720px]:px-4 ${wide ? "h-full" : ""}`}>
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-rule pb-[18px]">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Scouts</h1>
          <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">
            {project ?? "all projects"} · {rows.length} {rows.length === 1 ? "scout" : "scouts"}
            <Slash />
            <b className="font-semibold text-ink">{countScouts(rows, "running")}</b> running
            <Slash />
            {countScouts(rows, "queued")} queued
            {!wide && <><Slash />newest first</>}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-2">
            <SearchField label="Search scouts" />
            <FilterSelect label="State" param="filter" allLabel="All states" options={STATE_OPTIONS} />
            <FilterSelect label="Kind" param="type" allLabel="All kinds" options={KIND_OPTIONS} />
          </div>
        </div>
        <Btn
          variant="primary"
          expanded={open}
          disabled={!canLaunch}
          title={canLaunch ? undefined : ACTIONS_OFF}
          onClick={() => (open ? close() : setOpen(true))}
        >
          New scout
        </Btn>
      </header>
      {wide ? (
        <div className="flex min-h-0 flex-1 items-stretch gap-4">
          <ScoutList groups={groups} empty={empty} current={launching || shown == null ? null : scoutKey(shown)} />
          <div data-scout-pane className="min-w-0 flex-1 overflow-y-auto border border-rule bg-paper">
            {launching ? form(true) : shown != null && <ScoutPane key={scoutKey(shown)} project={shown.project} id={shown.id} />}
          </div>
        </div>
      ) : (
        <>
          {launching && form(false)}
          {empty ?? <ScoutTable groups={groups} />}
        </>
      )}
    </section>
  );
}
