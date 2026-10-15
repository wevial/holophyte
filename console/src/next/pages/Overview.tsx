import { Fragment, type ReactNode } from "react";
import { ageOf } from "../../lib/attention";
import { formatClock, formatSettled } from "../../lib/format";
import { FocusCard, useHeldEntry } from "../components/FocusCard";
import { laneGroups, RunningLanes } from "../components/RunLane";
import { Section } from "../components/Section";
import { useOverview } from "../overview";

const FOLD_KEY = "holophyte.next.fold.";

function readOpen(id: string): boolean {
  try {
    return localStorage.getItem(FOLD_KEY + id) !== "folded";
  } catch {
    return true;
  }
}

function writeOpen(id: string, open: boolean): void {
  try {
    if (open) localStorage.removeItem(FOLD_KEY + id);
    else localStorage.setItem(FOLD_KEY + id, "folded");
  } catch {
    // Storage may be disabled; the fold still holds for this page.
  }
}

const counted = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/** "DEMO-2 is queued.", "DEMO-2 and DEMO-12 are queued.", and past three
 *  keys "DEMO-2, DEMO-3, DEMO-4 and 2 more are queued." */
function queuedSentence(keys: string[]): string {
  if (keys.length === 0) return "Nothing running and nothing queued.";
  if (keys.length === 1) return `Nothing running. ${keys[0]} is queued.`;
  const named = keys.length > 3 ? [...keys.slice(0, 3), `${keys.length - 3} more`] : keys;
  return `Nothing running. ${named.slice(0, -1).join(", ")} and ${named.at(-1)} are queued.`;
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="font-mono text-[13px] text-ink-3">{children}</p>;
}

function FoldingSection({
  id,
  label,
  count,
  hot,
  summary,
  empty,
  isEmpty = count === 0,
  children,
}: {
  id: string;
  label: string;
  count: number;
  hot?: boolean;
  summary?: string;
  empty: string;
  isEmpty?: boolean;
  children?: ReactNode;
}) {
  return (
    <Section label={label} count={count} hot={hot} summary={summary} defaultOpen={readOpen(id)} onToggle={(open) => writeOpen(id, open)}>
      {isEmpty ? <Empty>{empty}</Empty> : children}
    </Section>
  );
}

export function Overview() {
  const view = useOverview();
  const held = useHeldEntry(view.needs);
  const need = view.needs.length;
  const oldest = view.needs[0] ? ageOf(view.needs[0].item, view.now) : null;
  const waiting = view.needs.filter((entry) => entry.readyToMerge).length;
  const queued = view.queued.length;
  const newest = view.mergedToday[0];
  const lanes = laneGroups(view.hosts);
  const busy = lanes.reduce((sum, group) => sum + group.runs.length, 0);
  const workers = lanes.reduce((sum, group) => sum + group.workers, 0);
  return (
    <div className="flex max-w-[880px] flex-col gap-8 px-8 py-7">
      {view.answered && (
        <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
          <h1 className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">
            {need === 0 ? (
              <>
                <span className="text-live">Nothing</span> needs you.
              </>
            ) : (
              <>
                <span className="text-need">{need}</span> {need === 1 ? "needs" : "need"} you.
              </>
            )}
          </h1>
          <p data-count-line className="font-mono text-[13px] text-ink-2">
            {(
              [
                [view.running, "running"],
                [view.pullRequests.length, view.pullRequests.length === 1 ? "open PR" : "open PRs"],
                [queued, "queued"],
                [view.mergedToday.length, "merged today"],
              ] as const
            ).map(([count, words], index) => (
              <Fragment key={words}>
                {index > 0 && <span className="text-ink-3"> · </span>}
                <b className="font-semibold text-ink">{count}</b> {words}
              </Fragment>
            ))}
          </p>
        </header>
      )}
      <div className="flex flex-col gap-[44px]">
        <FoldingSection
          id="need"
          label="Needs you"
          count={need}
          hot
          summary={oldest == null ? (need === 0 ? "all clear" : undefined) : `oldest waiting ${formatSettled(oldest)}`}
          empty={view.project == null ? "The factory has it from here." : `Nothing in ${view.project} is waiting on you.`}
        >
          <FocusCard entries={view.needs} index={held.index} hold={held.hold} hosts={view.hosts} now={view.now} />
        </FoldingSection>
        <FoldingSection
          id="run"
          label="Running"
          count={view.running}
          summary={`${busy} of ${workers} workers busy · ${queued === 0 ? "nothing queued" : `${queued} queued`}`}
          empty={queuedSentence(view.queued.map((ticket) => ticket.ticket))}
          isEmpty={!lanes.some((group) => group.runs.length > 0)}
        >
          <RunningLanes groups={lanes} now={view.now} />
        </FoldingSection>
        <FoldingSection
          id="pr"
          label="Pull requests"
          count={view.pullRequests.length}
          summary={waiting === 0 ? "none wait on you" : `${counted(waiting, "waits", "wait")} on you`}
          empty="No pull requests open."
        />
        <FoldingSection
          id="done"
          label="Merged today"
          count={view.mergedToday.length}
          summary={newest ? `last at ${formatClock(newest.ended_ms)}` : undefined}
          empty="Nothing merged yet today."
        />
      </div>
    </div>
  );
}
