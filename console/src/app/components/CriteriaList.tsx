import { useMemo } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import type { RunDetail } from "../../lib/contract.gen";
import { criteriaGrid, type CriteriaGrid, type GridCell } from "../activity";
import { CriterionMark } from "./icons";

type GridColumn = CriteriaGrid["columns"][number];

const ROUND_MARKS: Record<GridCell["status"], { word: string; tone: string; path: string }> = {
  met: { word: "met", tone: "live", path: "M5 8.3 7.1 10.4 11 6.2" },
  not_met: { word: "not met", tone: "bad", path: "M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" },
  unwitnessed: { word: "unwitnessed", tone: "ask", path: "M6.2 6.3a1.9 1.9 0 1 1 2.7 1.7c-.6.3-.9.7-.9 1.2v.3M8 11.4v.1" },
};

const markName = (column: GridColumn, cell: GridCell) => `${column.label} · ${ROUND_MARKS[cell.status].word}`;

function RoundMark({ name, status }: { name: string; status: GridCell["status"] }) {
  const { tone, path } = ROUND_MARKS[status];
  return (
    <Tooltip delay={150}>
      <TooltipTrigger
        render={({ type: _button, "aria-describedby": _described, ...trigger }) => (
          <span {...trigger} tabIndex={0} role="img" aria-label={name} data-tone={tone} style={{ color: `var(--${tone})` }} className="inline-flex size-5 items-center justify-center rounded-[6px] hover:bg-hover">
            <svg viewBox="0 0 16 16" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="8" cy="8" r="6.5" />
              <path d={path} />
            </svg>
          </span>
        )}
      />
      <TooltipContent side="top" sideOffset={6} className="z-50 rounded-[6px] border border-rule-2 bg-raised px-2.5 py-1 text-[12px] font-medium whitespace-nowrap text-ink shadow-card">
        {name}
      </TooltipContent>
    </Tooltip>
  );
}

/** One slot per column that judged criteria, so marks line up across rows; a hairline joins neighbouring marks. */
function RoundMarks({ columns, cells }: { columns: GridColumn[]; cells: (GridCell | null)[] }) {
  const names = cells.map((cell, index) => (cell == null ? null : markName(columns[index]!, cell)));
  return (
    <span role="group" aria-label={names.filter(Boolean).join(", ")} data-round-marks className="ml-auto inline-flex shrink-0 items-center">
      {cells.map((cell, index) => (
        <span key={columns[index]!.label} className="inline-flex items-center">
          {index > 0 && <i aria-hidden="true" className={`h-px w-1.5 ${cell != null && cells[index - 1] != null ? "bg-rule" : ""}`} />}
          {cell == null ? <span data-slot className="inline-block size-5" /> : <RoundMark name={names[index]!} status={cell.status} />}
        </span>
      ))}
    </span>
  );
}

export function CriteriaList({ detail, live }: { detail: RunDetail; live: boolean }) {
  const { criteria, rounds, run } = detail;
  const grid = useMemo(() => criteriaGrid(detail), [detail]);
  const met = criteria.filter((criterion) => criterion.status === "met").length;
  const active = rounds.find((round) => round.ended_ms == null);
  const pending = active?.round ?? Math.max(0, ...rounds.map((round) => round.round)) + 1;
  const notes = criteria.filter((criterion) => criterion.note.trim());
  const tests = notes.filter((criterion) => criterion.status === "met").length;
  const findings = notes.length - tests;
  const evidence = [tests ? `${tests} test${tests === 1 ? "" : "s"}` : "", findings ? `${findings} finding${findings === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
  return (
    <section aria-label="Acceptance criteria">
      <div className="flex items-center gap-2.5 font-sans text-[13px] font-semibold leading-none text-ink">
        Criteria
        {criteria.length > 0 && <span className="font-mono font-normal text-ink-3">{met} of {criteria.length} met</span>}
      </div>
      {criteria.length === 0 ? (
        <p className="mb-0 mt-3 font-mono text-[12px] text-ink-3">No review has judged the criteria yet.</p>
      ) : (
        <>
          <ol className="m-0 mt-3 list-none border-t border-rule-2 p-0">
            {criteria.map((criterion) => {
              const cells = grid?.rows.find((row) => row.id === criterion.id)?.cells ?? grid?.columns.map(() => null);
              const waiting = live && (criterion.run !== run.id || (criterion.status !== "met" && active != null && criterion.round < active.round));
              return (
                <li key={criterion.id} className="flex flex-wrap items-center gap-3 border-b border-rule-2 py-3">
                  <CriterionMark status={criterion.status} />
                  <strong className="w-[34px] shrink-0 font-mono text-[11px] font-bold text-ink-3">{criterion.id}</strong>
                  <span className="min-w-0 flex-1 font-read text-[14px] text-ink-2">{criterion.text}</span>
                  {waiting && <span className="font-mono text-[11px] text-ink-3">waits on round {pending}</span>}
                  {grid != null && cells != null && <RoundMarks columns={grid.columns} cells={cells} />}
                </li>
              );
            })}
          </ol>
          <details className="mt-3.5 border-t border-rule">
            <summary className="cursor-pointer py-3 font-mono text-[11px] font-medium text-ink-2">
              Evidence{evidence && <> <span className="ml-2.5 text-ink-3">{evidence}</span></>}
            </summary>
            <ul className="m-0 list-none whitespace-pre-wrap p-0 font-mono text-[12px] leading-[1.8] text-ink-2 [overflow-wrap:anywhere]">
              {notes.map((criterion) => (
                <li key={criterion.id}><strong>{criterion.id}</strong> · {criterion.status !== "met" && `round ${criterion.round}: `}{criterion.note}</li>
              ))}
            </ul>
          </details>
        </>
      )}
    </section>
  );
}
