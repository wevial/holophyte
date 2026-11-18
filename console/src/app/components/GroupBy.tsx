import type { GroupMode } from "../tickets";
import { Segmented } from "./Segmented";

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
      <Segmented label="Group by" options={modes.map((each) => ({ value: each, label: LABELS[each] }))} value={mode} onPick={onPick} />
    </div>
  );
}
