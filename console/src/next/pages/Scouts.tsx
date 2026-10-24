import { useState } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import { projectName } from "../../lib/derive";
import { formatSettled } from "../../lib/format";
import { Btn } from "../components/Btn";
import { EMPTY_DRAFT, ScoutLaunch, type LaunchProject } from "../components/ScoutLaunch";
import { StateDot } from "../components/StateDot";
import { firstLine } from "../history";
import { scopedHosts, useConsolePeers } from "../scope";
import { countScouts, scoutState, useScouts, type ScoutListRow, type ScoutState } from "../scouts";
import type { State } from "../state";

const GRID = "min-[720px]:grid min-[720px]:grid-cols-[20px_44px_minmax(0,1fr)_76px_minmax(0,96px)_minmax(0,64px)_minmax(0,112px)_48px] min-[720px]:items-start min-[720px]:gap-3";
const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";

const DOTS: Record<ScoutState, { state: State; label: string }> = {
  running: { state: "running", label: "running" },
  queued: { state: "queued", label: "queued" },
  done: { state: "merged", label: "done" },
  stopped: { state: "abandoned", label: "stopped" },
  failed: { state: "failed", label: "failed" },
};

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
    <li
      data-row
      data-scout={row.id}
      className={`${GRID} grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-1 border-b border-rule-2 px-3 py-3 text-[14px] transition-colors duration-(--motion-colour) last:border-b-0 hover:bg-paper-2 ${quiet ? "text-ink-2" : "text-ink"}`}
    >
      <span data-cell="state" className="pt-0.5"><StateDot {...DOTS[state]} hideWord /></span>
      <span data-cell="ask" className={`min-w-0 font-read text-[15px] font-medium leading-[1.35] min-[720px]:order-2 ${quiet ? "text-ink-2" : "text-ink"}`}>
        {row.ask}
        {reason && <span data-reason className="ml-2 font-mono text-[12.5px] font-semibold text-bad">{reason}</span>}
      </span>
      <span className={`col-start-2 flex flex-wrap ${MONO} min-[720px]:contents`}>
        <span data-cell="id" className={`${FACT} min-[720px]:order-1 min-[720px]:text-ink-2`}>{row.id}</span>
        <span data-cell="kind" className={`${FACT} min-[720px]:order-3`}>
          <span className="min-[720px]:border min-[720px]:border-rule min-[720px]:px-1 min-[720px]:text-[11px] min-[720px]:font-semibold min-[720px]:text-ink-2">{row.kind}</span>
        </span>
        <span data-cell="project" className={`${FACT} min-[720px]:order-4`}>{row.project}</span>
        <span data-cell="lead" className={`${FACT} min-[720px]:order-5`}>{row.lead ?? DASH}</span>
        <span data-cell="helpers" className={`${FACT} min-[720px]:order-6`}>{helpers(row)}</span>
        <span data-cell="took" className={`${FACT} whitespace-nowrap min-[720px]:order-7 min-[720px]:text-right`}>{took}</span>
      </span>
    </li>
  );
}

function ScoutTable({ rows, empty }: { rows: ScoutListRow[]; empty: string }) {
  return (
    <div data-scout-table className="border border-rule bg-paper">
      <div aria-hidden="true" className={`${GRID} hidden border-b border-rule px-3 pb-2.5 pt-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3`}>
        <span />
        <span>#</span>
        <span>Ask</span>
        <span>Kind</span>
        <span>Project</span>
        <span>Lead</span>
        <span>Helpers</span>
        <span className="text-right">Took</span>
      </div>
      <ol aria-label="Scouts, newest first" className="m-0 list-none p-0">
        {rows.map((row) => (
          <ScoutTableRow key={`${row.base} ${row.id}`} row={row} />
        ))}
      </ol>
      {rows.length === 0 && <p className="m-0 px-3 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3">{empty}</p>}
    </div>
  );
}

export function Scouts() {
  const { project, rows } = useScouts();
  const { hosts } = useConsolePeers();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const projects: LaunchProject[] = scopedHosts(hosts, project).flatMap((host) =>
    host.status?.actions === true ? [{ base: host.base, name: projectName(host.status.project) }] : [],
  );
  const canLaunch = projects.length > 0;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-2">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-rule pb-[18px]">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Scouts</h1>
          <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">
            {project ?? "all projects"} · {rows.length} {rows.length === 1 ? "scout" : "scouts"}
            <Slash />
            <b className="font-semibold text-ink">{countScouts(rows, "running")}</b> running
            <Slash />
            {countScouts(rows, "queued")} queued
            <Slash />
            newest first
          </p>
        </div>
        <Btn
          variant="primary"
          expanded={open}
          disabled={!canLaunch}
          title={canLaunch ? undefined : ACTIONS_OFF}
          onClick={() => setOpen(!open)}
        >
          New scout
        </Btn>
      </header>
      {open && canLaunch && (
        <ScoutLaunch
          projects={projects}
          draft={draft}
          onDraft={setDraft}
          onCancel={() => setOpen(false)}
          onLaunched={() => {
            setOpen(false);
            setDraft(EMPTY_DRAFT);
          }}
        />
      )}
      <ScoutTable rows={rows} empty={`No scouts in ${project ?? "any project"}.`} />
    </section>
  );
}
