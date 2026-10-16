import { useId } from "react";
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
  const label = useId();
  return (
    <div className="flex items-center gap-2.5">
      <span id={label} className="font-mono text-[11px] font-medium uppercase tracking-[0.12em] text-ink-3">
        Group by
      </span>
      <div role="group" aria-labelledby={label} className="inline-flex border border-ink-3">
        {modes.map((each) => (
          <button
            key={each}
            type="button"
            aria-pressed={each === mode}
            onClick={() => onPick(each)}
            className={`min-h-9 cursor-pointer px-3 font-mono text-[11px] font-medium uppercase tracking-[0.1em] ${each === mode ? "bg-ink text-paper" : "text-ink-2"}`}
          >
            {LABELS[each]}
          </button>
        ))}
      </div>
    </div>
  );
}
