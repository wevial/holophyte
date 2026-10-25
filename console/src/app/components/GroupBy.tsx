import type { GroupMode } from "../tickets";

const GROUP_KEY = "holophyte.next.tickets.group";

const LABELS: Record<GroupMode, string> = { project: "Project", story: "Story", none: "None" };

export function readGroup(): GroupMode {
  try {
    const stored = localStorage.getItem(GROUP_KEY);
    return stored === "project" || stored === "story" ? stored : "none";
  } catch {
    return "none";
  }
}

export function writeGroup(mode: GroupMode): void {
  try {
    localStorage.setItem(GROUP_KEY, mode);
  } catch {
    // Storage may be disabled; the choice still holds for this page.
  }
}

export function GroupBy({ modes, mode, onPick }: { modes: GroupMode[]; mode: GroupMode; onPick: (mode: GroupMode) => void }) {
  return (
    <div className="flex items-center gap-2">
      <span aria-hidden="true" className="font-sans text-[12.5px] text-ink-3">
        Group
      </span>
      <div role="group" aria-label="Group by" className="inline-flex border border-rule bg-paper [&>*+*]:border-l [&>*+*]:border-rule">
        {modes.map((each) => (
          <button
            key={each}
            type="button"
            aria-pressed={each === mode}
            onClick={() => onPick(each)}
            className={`inline-flex min-h-8 cursor-pointer items-center px-3 font-sans text-[12.5px] leading-none ${
              each === mode ? "bg-act-wash text-act" : "text-ink-3 hover:text-ink"
            }`}
          >
            {LABELS[each]}
          </button>
        ))}
      </div>
    </div>
  );
}
