import { Fragment, useContext, useRef, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { ageOf } from "../../lib/attention";
import { formatClock, formatSettled } from "../../lib/format";
import { DayLog } from "../components/DayLog";
import { FocusCard, useHeldEntry } from "../components/FocusCard";
import { MergedToday } from "../components/MergedToday";
import { FirstLoad, Notices } from "../components/Notices";
import { prLaneGroups, PullRequestLanes } from "../components/PrLane";
import { laneGroups, RunningLanes } from "../components/RunLane";
import { Section } from "../components/Section";
import { firstLoadLine, noticeLines, silentHosts, staleConsole, tokenWanted } from "../notices";
import { useOverview } from "../overview";
import { useDayLedger, usePeerList } from "../queries";
import { OriginContext, PollDepsContext, useConsolePeers } from "../scope";

const FOLD_KEY = "holophyte.next.fold.";
const FOLDED_BY_DEFAULT = new Set(["done"]);

function readOpen(id: string): boolean {
  const fallback = !FOLDED_BY_DEFAULT.has(id);
  try {
    const stored = localStorage.getItem(FOLD_KEY + id);
    return stored === "open" ? true : stored === "folded" ? false : fallback;
  } catch {
    return fallback;
  }
}

function writeOpen(id: string, open: boolean): void {
  try {
    localStorage.setItem(FOLD_KEY + id, open ? "open" : "folded");
  } catch {
    // Storage may be disabled; the fold still holds for this page.
  }
}

const COLUMN = "flex min-w-0 flex-col gap-[44px] min-[1600px]:row-start-2 min-[1600px]:@container/ov-col";

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
  loading = false,
  open = readOpen(id),
  onFold,
  children,
}: {
  id: string;
  label: string;
  count: number;
  hot?: boolean;
  summary?: string;
  empty: string;
  isEmpty?: boolean;
  loading?: boolean;
  open?: boolean;
  onFold?: (open: boolean) => void;
  children?: ReactNode;
}) {
  return (
    <Section label={label} count={loading ? undefined : count} hot={hot} summary={loading ? undefined : summary} defaultOpen={open}
      onToggle={(next) => {
        writeOpen(id, next);
        onFold?.(next);
      }}
    >
      {loading ? null : isEmpty ? <Empty>{empty}</Empty> : children}
    </Section>
  );
}

export function Overview() {
  const view = useOverview();
  const deps = useContext(PollDepsContext);
  const log = useDayLedger(view.hosts, view.now, deps);
  const origin = useContext(OriginContext);
  const { hosts: everyHost } = useConsolePeers();
  const peers = usePeerList(origin, everyHost.length === 0, deps);
  const asking = firstLoadLine(view.hosts, origin, peers);
  const firstLoad = asking != null;
  const held = useHeldEntry(view.needs);
  const need = view.needs.length;
  const oldest = view.needs[0] ? ageOf(view.needs[0].item, view.now) : null;
  const waiting = view.needs.filter((entry) => entry.readyToMerge).length;
  const queued = view.queued.length;
  const newest = view.mergedToday[0];
  const lanes = laneGroups(view.hosts);
  const busy = lanes.reduce((sum, group) => sum + group.runs.length, 0);
  const workers = lanes.reduce((sum, group) => sum + group.workers, 0);
  const prLanes = prLaneGroups(view.hosts, view.pullRequests, view.now);
  const card = useRef<HTMLElement>(null);
  const [needMount, setNeedMount] = useState(0);
  const [needOpen, setNeedOpen] = useState(() => readOpen("need"));
  const showNeed = (index: number) => {
    flushSync(() => {
      if (!needOpen) {
        writeOpen("need", true);
        setNeedOpen(true);
        setNeedMount((count) => count + 1);
      }
      held.hold(index);
    });
    card.current?.focus();
  };
  return (
    <div className="flex flex-col gap-8 px-8 py-7 min-[1200px]:flex-row min-[1200px]:items-start min-[1600px]:grid min-[1600px]:grid-cols-[minmax(0,1.08fr)_minmax(0,1fr)_330px] min-[1600px]:grid-rows-[auto_1fr] min-[1600px]:gap-x-[36px] min-[1600px]:gap-y-[32px]">
      <div className="flex min-w-0 max-w-[880px] flex-1 flex-col gap-8 min-[1600px]:contents">
        <div data-overview-head className="contents min-[1600px]:col-span-2 min-[1600px]:col-start-1 min-[1600px]:row-start-1 min-[1600px]:flex min-[1600px]:min-w-0 min-[1600px]:flex-col min-[1600px]:gap-8">
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
        {asking != null && tokenWanted(everyHost).length === 0 && <FirstLoad line={asking} />}
        <Notices
          stale={staleConsole(everyHost, origin)}
          silent={silentHosts(view.hosts, view.now)}
          lines={noticeLines(view.hosts, log.answers, view.now)}
        />
        </div>
        <div className="flex flex-col gap-[44px] min-[1600px]:contents">
          <div data-overview-column className={`${COLUMN} min-[1600px]:col-start-1`}>
          <FoldingSection
            loading={firstLoad}
            key={needMount}
            id="need"
            open={needOpen}
            onFold={setNeedOpen}
            label="Needs you"
            count={need}
            hot
            summary={oldest == null ? (need === 0 ? "all clear" : undefined) : `oldest waiting ${formatSettled(oldest)}`}
            empty={view.project == null ? "The factory has it from here." : `Nothing in ${view.project} is waiting on you.`}
          >
            <FocusCard entries={view.needs} index={held.index} hold={held.hold} hosts={view.hosts} now={view.now} cardRef={card} />
          </FoldingSection>
          <FoldingSection
            loading={firstLoad}
            id="run"
            label="Running"
            count={view.running}
            summary={`${busy} of ${workers} workers busy · ${queued === 0 ? "nothing queued" : `${queued} queued`}`}
            empty={queuedSentence(view.queued.map((ticket) => ticket.ticket))}
            isEmpty={!lanes.some((group) => group.runs.length > 0)}
          >
            <RunningLanes groups={lanes} now={view.now} />
          </FoldingSection>
          </div>
          <div data-overview-column className={`${COLUMN} min-[1600px]:col-start-2`}>
          <FoldingSection
            loading={firstLoad}
            id="pr"
            label="Pull requests"
            count={view.pullRequests.length}
            summary={waiting === 0 ? "none wait on you" : `${counted(waiting, "waits", "wait")} on you`}
            empty="No pull requests open."
            isEmpty={!prLanes.some((group) => group.rows.length > 0)}
          >
            <PullRequestLanes groups={prLanes} readiness={view.readiness} needs={view.needs} onShow={showNeed} />
          </FoldingSection>
          <FoldingSection
            loading={firstLoad}
            id="done"
            label="Merged today"
            count={view.mergedToday.length}
            summary={newest ? `last at ${formatClock(newest.ended_ms)}` : undefined}
            empty="Nothing merged yet today."
          >
            <MergedToday rows={view.mergedToday} />
          </FoldingSection>
          </div>
        </div>
      </div>
      <div className="min-w-0 max-w-[880px] min-[1200px]:flex-[0_0_290px] min-[1600px]:sticky min-[1600px]:top-0 min-[1600px]:self-start min-[1600px]:h-screen min-[1600px]:-mb-7 min-[1600px]:col-start-3 min-[1600px]:row-span-2 min-[1600px]:row-start-1 min-[1600px]:w-[330px]">
        <DayLog entries={log.entries} />
      </div>
    </div>
  );
}
