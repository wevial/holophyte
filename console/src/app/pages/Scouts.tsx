import { useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { useAction } from "../actions";
import { formatSettled } from "../../lib/format";
import { Btn } from "../components/Btn";
import { FactIcon } from "../components/icons";
import { PageHeader } from "../components/PageHeader";
import { EMPTY_DRAFT, prefilledDraft, ScoutLaunch } from "../components/ScoutLaunch";
import { EMPTY_LINE, NEW_SCOUT, PLUS, ScoutFilters, ScoutGroups, ScoutsBeside, ShowScoutList, useLaunchProjects } from "../components/ScoutsSidebar";
import { StateDot } from "../components/StateDot";
import { firstLine } from "../history";
import type { ScopeSearch } from "../scope";
import { SCOUT_DOTS, scoutPage } from "../scout";
import {
  cappedReason,
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
const FACTS = "font-sans text-[13px] leading-[1.4] text-ink-3";
const DASH = "–";

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
    <li data-row data-scout={row.id} className="border-b border-rule-2">
      <Link
        {...scoutPage(row.project, row.id)}
        className={`${GRID} grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-1 px-2.5 py-2.5 text-[13px] no-underline transition-colors duration-(--motion-colour) hover:bg-hover ${quiet ? "text-ink-2" : "text-ink"}`}
      >
        <span data-cell="state" className="flex items-start pt-[3px]"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
        <span data-cell="title" className={`min-w-0 font-sans text-[14px] font-medium leading-[1.35] min-[720px]:order-2 ${quiet ? "text-ink-2" : "text-ink"}`}>
          {row.title}
          {reason && <span data-reason title={reason} className="ml-2 font-sans text-[13px] font-medium text-bad">{cappedReason(reason)}</span>}
        </span>
        <span className={`col-start-2 flex flex-wrap ${FACTS} min-[720px]:contents`}>
          <span data-cell="kind" className={`${FACT} min-[720px]:order-3`}>
            <span className="inline-flex h-5 items-center rounded-[5px] bg-selected px-[7px] align-middle text-[11.5px] font-semibold leading-none text-ink-2">{row.kind}</span>
          </span>
          <span data-cell="lead" className={`${FACT} min-[720px]:order-5`}>{row.lead ?? DASH}</span>
          <span data-cell="helpers" className={`${FACT} min-[720px]:order-6`}>{helpers(row)}</span>
          <span data-cell="took" className={`${FACT} whitespace-nowrap font-mono text-[12px] min-[720px]:order-7 min-[720px]:text-right`}>{took}</span>
        </span>
      </Link>
    </li>
  );
}

function ScoutTable({ groups }: { groups: ScoutGroup[] }) {
  return (
    <div data-scout-table className="flex flex-col">
      <div aria-hidden="true" className={`${GRID} hidden px-2.5 pb-2 font-sans text-[12px] font-medium leading-none text-ink-3`}>
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
      className={NEW_SCOUT}
      expanded={open}
      disabled={!canLaunch}
      title={canLaunch ? undefined : ACTIONS_OFF}
      onClick={() => (open ? close() : setOpen(true))}
    >
      {PLUS}
      New scout
    </Btn>
  );
  const shown = launching ? undefined : rows[0];
  if (wide) {
    return (
      <ScoutsBeside search={search} current={shown == null ? null : scoutKey(shown)} newScout={newScout}>
        {shown == null ? (
          <div className="max-w-[820px]">
            <ShowScoutList row />
            {launching && form(true)}
          </div>
        ) : <ScoutView key={scoutKey(shown)} project={shown.project} param={String(shown.id)} />}
      </ScoutsBeside>
    );
  }
  const narrowed = search.q != null || search.filter != null || search.type != null;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-6 pt-3.5 pb-7 max-[720px]:px-4">
      <div className="flex flex-col gap-3">
        <PageHeader title="Scouts" count={{ text: `${rows.length} ${rows.length === 1 ? "scout" : "scouts"} · ${countScouts(rows, "running")} running` }} tools={newScout} />
        <div className="flex flex-wrap items-center gap-2 [&>div]:shrink-0">
          <ScoutFilters searchWidth="min-w-[120px] max-w-56 grow basis-[120px]" />
        </div>
      </div>
      {launching && form(false)}
      {rows.length > 0 ? <ScoutTable groups={scoutGroups(rows)} /> : <p className={EMPTY_LINE}><FactIcon ok />{narrowed ? "No scouts match" : `No scouts in ${project ?? "any project"}.`}</p>}
    </section>
  );
}
