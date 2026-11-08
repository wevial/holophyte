import { useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { useAction } from "../actions";
import { formatSettled } from "../../lib/format";
import { Btn } from "../components/Btn";
import { EMPTY_DRAFT, prefilledDraft, ScoutLaunch } from "../components/ScoutLaunch";
import { EMPTY_LINE, ScoutFilters, ScoutGroups, ScoutsBeside, ShowScoutList, useLaunchProjects } from "../components/ScoutsSidebar";
import { StateDot } from "../components/StateDot";
import { firstLine } from "../history";
import type { ScopeSearch } from "../scope";
import { SCOUT_DOTS, scoutPage } from "../scout";
import {
  countScouts,
  keepList,
  scoutGroups,
  scoutKey,
  scoutState,
  shownScouts,
  useScouts,
  type ScoutGroup,
  type ScoutListRow,
  type ScoutsSearch,
} from "../scouts";
import { useScoutListWidth } from "../viewport";
import { ScoutView } from "./Scout";

const GRID = "min-[720px]:grid min-[720px]:grid-cols-[16px_minmax(0,1fr)_76px_52px_88px_56px] min-[720px]:items-start min-[720px]:gap-3";
const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";

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

export function Scouts() {
  const { project, rows: listed } = useScouts();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const launch = useAction();
  const { scoped, projects } = useLaunchProjects(project);
  const canLaunch = projects.length > 0;
  const search = useSearch({ from: "/scouts" });
  const rows = shownScouts(listed, search);
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
  const wide = useScoutListWidth();
  const close = () => {
    setOpen(false);
    if (asked != null) void navigate({ to: "/scouts", search: (previous: ScoutsSearch & ScopeSearch) => keepList(previous), replace: true });
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
  const newScout = (
    <Btn
      variant="primary"
      expanded={open}
      disabled={!canLaunch}
      title={canLaunch ? undefined : ACTIONS_OFF}
      onClick={() => (open ? close() : setOpen(true))}
    >
      New scout
    </Btn>
  );
  const first = rows[0];
  if (wide) {
    return (
      <ScoutsBeside search={search} current={launching || first == null ? null : scoutKey(first)} newScout={newScout}>
        {launching ? (
          <div className="max-w-[820px]">
            <div className="px-6 pt-6 empty:hidden"><ShowScoutList /></div>
            {form(true)}
          </div>
        ) : first != null && <ScoutView key={scoutKey(first)} project={first.project} param={String(first.id)} />}
      </ScoutsBeside>
    );
  }
  const narrowed = search.q != null || search.filter != null || search.type != null;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-6 pt-3.5 pb-7 max-[720px]:px-4">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-rule pb-[18px]">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Scouts</h1>
          <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">
            {project ?? "all projects"} · {rows.length} {rows.length === 1 ? "scout" : "scouts"}
            <Slash />
            <b className="font-semibold text-ink">{countScouts(rows, "running")}</b> running
            <Slash />
            {countScouts(rows, "queued")} queued
            <Slash />newest first
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-2">
            <ScoutFilters />
          </div>
        </div>
        {newScout}
      </header>
      {launching && form(false)}
      {rows.length > 0 ? <ScoutTable groups={scoutGroups(rows)} /> : <p className={EMPTY_LINE}>{narrowed ? "No scouts match" : `No scouts in ${project ?? "any project"}.`}</p>}
    </section>
  );
}
