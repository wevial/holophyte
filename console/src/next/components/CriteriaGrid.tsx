import { useId } from "react";
import type { CriteriaGrid as Grid, GridCell } from "../activity";

const WORD: Record<GridCell["status"], string> = { met: "met", not_met: "not met", unwitnessed: "unwitnessed" };

const CELL_TONE: Record<GridCell["status"], string> = {
  met: "text-live",
  not_met: "bg-bad-wash text-bad",
  unwitnessed: "text-ask-ink",
};

const EDGE = "border border-rule";
const OUTLINE = "border-x-2 border-x-bad";

function Glyph({ status }: { status: GridCell["status"] }) {
  if (status === "unwitnessed") return <span className="font-mono text-[14px] font-bold leading-none">?</span>;
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="inline-block">
      <path d={status === "met" ? "M3.5 8.4 6.6 11.5 12.5 5" : "M4.5 4.5l7 7M11.5 4.5l-7 7"} />
    </svg>
  );
}

function Cell({ cell, failed, last }: { cell: GridCell | null; failed: boolean; last: boolean }) {
  const name = cell == null ? undefined : cell.note ? `${WORD[cell.status]}: ${cell.note}` : WORD[cell.status];
  const outline = failed ? `${OUTLINE} ${last ? "border-b-2 border-b-bad" : ""}` : "";
  return (
    <td
      aria-label={name}
      title={name}
      data-status={cell?.status}
      data-failed={failed ? "" : undefined}
      className={`${EDGE} ${outline} h-9 min-w-[64px] px-3 text-center ${cell == null ? "" : CELL_TONE[cell.status]}`}
    >
      {cell != null && <Glyph status={cell.status} />}
    </td>
  );
}

function LegendItem({ status }: { status: GridCell["status"] }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden="true" className={`${EDGE} inline-flex size-7 items-center justify-center ${CELL_TONE[status]}`}>
        <Glyph status={status} />
      </span>
      {WORD[status]}
    </span>
  );
}

/** Criteria as rows against the pre-check, each review round and the final check as columns. */
export function CriteriaGrid({ grid }: { grid: Grid }) {
  const label = useId();
  return (
    <section data-criteria-grid className="mt-4">
      <div id={label} className="font-sans text-[13px] font-semibold text-ink-2">Criteria by round</div>
      <div className="mt-2 overflow-x-auto">
        <table aria-labelledby={label} className="border-collapse font-mono text-[13px] text-ink">
          <thead>
            <tr>
              <td className={`${EDGE} bg-paper-2`} />
              {grid.columns.map((column) => (
                <th
                  key={column.label}
                  scope="col"
                  data-failed={column.failed ? "" : undefined}
                  className={`${EDGE} ${column.failed ? `${OUTLINE} border-t-2 border-t-bad` : ""} bg-paper-2 px-3 py-2 font-semibold whitespace-nowrap`}
                >
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.map((row, index) => (
              <tr key={row.id}>
                <th scope="row" className={`${EDGE} px-3 text-left font-medium text-ink-2`}>{row.id}</th>
                {row.cells.map((cell, column) => (
                  <Cell key={grid.columns[column]!.label} cell={cell} failed={grid.columns[column]!.failed} last={index === grid.rows.length - 1} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div data-legend className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2 font-sans text-[14px] text-ink-2">
        <LegendItem status="met" />
        <LegendItem status="not_met" />
        <LegendItem status="unwitnessed" />
      </div>
    </section>
  );
}
