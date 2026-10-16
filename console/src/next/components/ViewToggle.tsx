const VIEW_KEY = "holophyte.next.tickets.view";

export type TicketView = "board" | "table";

export function readView(): TicketView {
  try {
    return localStorage.getItem(VIEW_KEY) === "table" ? "table" : "board";
  } catch {
    return "board";
  }
}

export function writeView(view: TicketView): void {
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    // Storage may be disabled; the choice still holds for this page.
  }
}

const VIEWS: { view: TicketView; label: string; glyph: string }[] = [
  { view: "board", label: "Board", glyph: "M2.5 2.5h3v11h-3zM6.5 2.5h3v8h-3zM10.5 2.5h3v5h-3z" },
  { view: "table", label: "Table", glyph: "M2.5 4h11M2.5 8h11M2.5 12h11" },
];

export function ViewToggle({ view, onPick }: { view: TicketView; onPick: (view: TicketView) => void }) {
  return (
    <div role="group" aria-label="View" className="inline-flex border border-rule bg-paper [&>*+*]:border-l [&>*+*]:border-rule">
      {VIEWS.map((each) => (
        <button
          key={each.view}
          type="button"
          aria-pressed={each.view === view}
          onClick={() => onPick(each.view)}
          className={`inline-flex min-h-9 cursor-pointer items-center gap-[7px] px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.08em] ${
            each.view === view ? "bg-act-wash text-act" : "text-ink-3 hover:text-ink"
          }`}
        >
          <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-3.5 shrink-0">
            <path d={each.glyph} />
          </svg>
          {each.label}
        </button>
      ))}
    </div>
  );
}
