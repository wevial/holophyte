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

const VIEWS: { view: TicketView; label: string }[] = [
  { view: "board", label: "Board" },
  { view: "table", label: "Table" },
];

export function ViewToggle({ view, onPick }: { view: TicketView; onPick: (view: TicketView) => void }) {
  return (
    <div role="group" aria-label="View" className="flex gap-0.5">
      {VIEWS.map((each) => (
        <button
          key={each.view}
          type="button"
          aria-pressed={each.view === view}
          onClick={() => onPick(each.view)}
          className="inline-flex h-7 cursor-pointer items-center rounded-[6px] px-2.5 font-sans text-[13px] font-medium leading-none text-ink-2 transition-colors hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink"
        >
          {each.label}
        </button>
      ))}
    </div>
  );
}
