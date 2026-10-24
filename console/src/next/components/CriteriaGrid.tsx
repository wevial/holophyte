import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import type { CriteriaGrid as Grid, GridCell } from "../activity";

const WORD: Record<GridCell["status"], string> = { met: "met", not_met: "not met", unwitnessed: "unwitnessed" };

const CELL_TONE: Record<GridCell["status"], string> = {
  met: "text-live",
  not_met: "bg-bad-wash text-bad",
  unwitnessed: "text-warn-ink",
};

const EDGE = "border border-rule";
const RAIL_CELL = "@[640px]/progress:min-w-0 @[640px]/progress:px-1.5";
const MARGIN = 8;
const GAP = 6;

function Glyph({ status }: { status: GridCell["status"] }) {
  if (status === "unwitnessed") return <span className="font-mono text-[14px] font-bold leading-none">?</span>;
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="inline-block">
      <path d={status === "met" ? "M3.5 8.4 6.6 11.5 12.5 5" : "M4.5 4.5l7 7M11.5 4.5l-7 7"} />
    </svg>
  );
}

function Cell({ cell }: { cell: GridCell | null }) {
  const name = cell == null ? undefined : cell.note ? `${WORD[cell.status]}: ${cell.note}` : WORD[cell.status];
  return (
    <td
      aria-label={name}
      title={name}
      data-status={cell?.status}
      className={`${EDGE} ${RAIL_CELL} h-9 min-w-[64px] px-3 text-center ${cell == null ? "" : CELL_TONE[cell.status]}`}
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

/** Below the label, or above it when there is no room below, and never past the viewport's sides. */
function useTipPlace(shown: boolean) {
  const anchor = useRef<HTMLButtonElement>(null);
  const tip = useRef<HTMLSpanElement>(null);
  const [place, setPlace] = useState<CSSProperties>({});
  useLayoutEffect(() => {
    if (!shown || anchor.current == null || tip.current == null) return;
    const at = anchor.current.getBoundingClientRect();
    const size = tip.current.getBoundingClientRect();
    const left = Math.max(MARGIN, Math.min(at.left, window.innerWidth - size.width - MARGIN));
    const below = at.bottom + GAP;
    const top = below + size.height > window.innerHeight - MARGIN ? Math.max(MARGIN, at.top - size.height - GAP) : below;
    setPlace({ left, top });
  }, [shown]);
  return { anchor, tip, place };
}

interface LabelProps {
  id: string;
  text: string;
  shown: boolean;
  onShow: (shown: boolean) => void;
  onToggle: () => void;
}

/** A criterion's id that shows its full text while hovered or focused; a touch or key press toggles it,
 *  and a scroll hides it rather than leave it behind its label. */
function RowLabel({ id, text, shown, onShow, onToggle }: LabelProps) {
  const tipId = useId();
  const pointer = useRef<string | null>(null);
  const { anchor, tip, place } = useTipPlace(shown);
  useEffect(() => {
    if (!shown) return;
    const hide = () => onShow(false);
    window.addEventListener("scroll", hide, { capture: true, passive: true });
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, { capture: true });
      window.removeEventListener("resize", hide);
    };
  }, [shown, onShow]);
  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-describedby={shown ? tipId : undefined}
        onPointerEnter={(event) => event.pointerType !== "touch" && onShow(true)}
        onPointerLeave={(event) => event.pointerType !== "touch" && onShow(false)}
        onPointerDown={(event) => (pointer.current = event.pointerType)}
        onPointerCancel={() => (pointer.current = null)}
        onFocus={() => pointer.current == null && onShow(true)}
        onBlur={() => onShow(false)}
        onKeyDown={(event) => event.key === "Escape" && onShow(false)}
        onClick={() => {
          const by = pointer.current;
          pointer.current = null;
          if (by === "mouse") onShow(true);
          else onToggle();
        }}
        className="cursor-help border-0 bg-transparent p-0 font-mono text-[13px] font-bold text-ink"
      >
        {id}
      </button>
      {shown && (
        <span
          ref={tip}
          id={tipId}
          role="tooltip"
          style={place}
          className="fixed z-20 w-max max-w-[min(320px,calc(100vw-16px))] border border-rule bg-paper px-3 py-2 text-left font-sans text-[13px] font-normal leading-[1.45] text-ink shadow-[var(--card-shadow)]"
        >
          {text}
        </span>
      )}
    </>
  );
}

/** Criteria as rows against the pre-check, each review round and the final check as columns; in the
 *  side rail the column headers shorten, keeping their full names. */
export function CriteriaGrid({ grid }: { grid: Grid }) {
  const label = useId();
  const [tip, setTip] = useState<string | null>(null);
  return (
    <section data-criteria-grid>
      <div id={label} className="font-sans text-[13px] font-semibold text-ink-2">Criteria by round</div>
      <div className="mt-2 overflow-x-auto">
        <table aria-labelledby={label} className="border-collapse font-mono text-[13px] text-ink @[640px]/progress:w-full">
          <thead>
            <tr>
              <td className={`${EDGE} bg-paper-2`} />
              {grid.columns.map((column) => (
                <th key={column.label} scope="col" aria-label={column.label} title={column.label} className={`${EDGE} ${RAIL_CELL} bg-paper-2 px-3 py-2 font-semibold whitespace-nowrap`}>
                  <span aria-hidden="true" className="@[640px]/progress:hidden">{column.label}</span>
                  <span aria-hidden="true" className="hidden @[640px]/progress:inline">{column.short}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.map((row) => (
              <tr key={row.id}>
                <th scope="row" className={`${EDGE} ${RAIL_CELL} px-3 text-left font-bold text-ink`}>
                  {row.text == null ? (
                    row.id
                  ) : (
                    <RowLabel
                      id={row.id}
                      text={row.text}
                      shown={tip === row.id}
                      onShow={(shown) => setTip((was) => (shown ? row.id : was === row.id ? null : was))}
                      onToggle={() => setTip((was) => (was === row.id ? null : row.id))}
                    />
                  )}
                </th>
                {row.cells.map((cell, column) => (
                  <Cell key={grid.columns[column]!.label} cell={cell} />
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
