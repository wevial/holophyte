import { useContext, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { projectColours } from "../dayLog";
import { DayLog } from "../components/DayLog";
import { FocusCard, useOpenEntry } from "../components/FocusCard";
import { MergedToday, SHOWN_MERGES } from "../components/MergedToday";
import { FirstLoad, Notices } from "../components/Notices";
import { PullRequestGlyph, waitingRows, WaitingRows } from "../components/PrLane";
import { laneGroups, liveRuns, RunningRows, witnessLines } from "../components/RunLane";
import { STATE_ICONS, TONE_TEXT } from "../components/StateDot";
import { firstLoadLine, noticeLines, silentHosts, staleConsole, tokenWanted } from "../notices";
import { useOverview } from "../overview";
import { useDayLedger, usePeerList } from "../queries";
import { keepScope, OriginContext, PollDepsContext, useConsolePeers, type ScopeSearch } from "../scope";
import type { Tone } from "../state";

function Glyph({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`size-3.5 shrink-0 ${className}`}>
      {children}
    </svg>
  );
}

function GlyphCount({ count, words, tone, icon }: { count: number; words: string; tone: Tone; icon: ReactNode }) {
  const label = `${count} ${words}`;
  return (
    <span role="img" aria-label={label} title={label} className="inline-flex items-center gap-1.5 font-mono text-[13px] text-ink-2">
      <Glyph className={TONE_TEXT[tone]}>{icon}</Glyph>
      {count}
    </span>
  );
}

function Rows({ label, count, aside, empty, footer, children }: { label: string; count?: number; aside?: ReactNode; empty: string; footer?: ReactNode; children?: ReactNode }) {
  return (
    <section aria-label={label} className="flex flex-col">
      <header className="flex min-h-8 items-baseline gap-2 border-b border-rule-2 px-2.5 pb-2">
        <h2 className="font-sans text-[14px] font-semibold text-ink">{label}</h2>
        {count != null && <span data-count className="font-mono text-[12px] text-ink-3">{count}</span>}
        {aside != null && <span className="ml-auto">{aside}</span>}
      </header>
      {count == null ? null : count === 0 ? <p className="px-2.5 py-2.5 font-mono text-[12px] text-ink-3">{empty}</p> : children}
      {count != null && footer}
    </section>
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
  const loaded = asking == null;
  const opened = useOpenEntry(view.needs);
  const need = view.needs.length;
  const lanes = laneGroups(view.hosts);
  const running = liveRuns(lanes, view.now);
  const witnesses = witnessLines(lanes, view.now);
  const waiting = waitingRows(view.hosts, view.pullRequests, view.now);
  const merged = view.mergedToday;
  const more = merged.length - SHOWN_MERGES;
  const shown = (count: number) => (loaded ? count : undefined);
  return (
    <div className="flex flex-col gap-8 px-8 pt-3.5 pb-7 min-[1200px]:flex-row min-[1200px]:items-start min-[1600px]:gap-[36px]">
      <div className="flex min-w-0 max-w-[880px] flex-1 flex-col gap-10">
        <div className="flex flex-col gap-6">
          {view.answered && (
            <header data-overview-head className="flex flex-col gap-3 pt-4">
              <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
                <h1 className="flex items-baseline gap-3 font-sans text-ink">
                  <span data-numeral className={`text-[56px] font-light leading-none tracking-[-0.02em] ${need > 0 ? "text-need" : "text-act"}`}>{need > 0 ? need : running.length}</span>{" "}
                  <span className="text-[22px] font-normal leading-none">{need > 0 ? "need you" : "running"}</span>
                </h1>
                <div data-glyph-counts className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <GlyphCount count={running.length} words="running" tone="live" icon={STATE_ICONS.running} />
                  <GlyphCount count={waiting.length} words="waiting on a pull request" tone="wait" icon={<PullRequestGlyph />} />
                  <GlyphCount count={view.queued.length} words="queued" tone="ink-2" icon={STATE_ICONS.queued} />
                  <GlyphCount count={merged.length} words="merged today" tone="live" icon={STATE_ICONS.merged} />
                </div>
              </div>
              {need === 0 && (
                <p className="flex items-center gap-2 font-sans text-[14px] text-ink-2">
                  <Glyph className="text-live">{STATE_ICONS.ready_to_merge}</Glyph>
                  Nothing needs you
                </p>
              )}
            </header>
          )}
          {asking != null && tokenWanted(everyHost).length === 0 && <FirstLoad line={asking} />}
          <Notices
            stale={staleConsole(everyHost, origin)}
            silent={silentHosts(view.hosts, view.now)}
            lines={noticeLines(view.hosts, log.answers, view.now)}
          />
        </div>
        {loaded && need > 0 && (
          <Rows label="Needs you" count={need} empty="">
            <FocusCard entries={view.needs} open={opened.open} toggle={opened.toggle} hosts={view.hosts} now={view.now} />
          </Rows>
        )}
        <Rows
          label="Running"
          count={shown(running.length)}
          empty="Nothing running."
          footer={witnesses.map((line) => (
            <p key={line} data-witnesses className="px-2.5 pt-2.5 font-mono text-[12px] text-ink-3">{line}</p>
          ))}
        >
          <RunningRows rows={running} />
        </Rows>
        <Rows label="Waiting" count={shown(waiting.length)} empty="Nothing waits on a pull request.">
          <WaitingRows rows={waiting} readiness={view.readiness} />
        </Rows>
        <Rows
          label="Merged today"
          count={shown(merged.length)}
          empty="Nothing merged yet today."
          aside={more > 0 ? (
            <Link to="/runs" search={(previous: ScopeSearch) => ({ ...keepScope(previous), state: "merged", range: "today" })} className="inline-flex items-center gap-1.5 font-sans text-[13px] text-ink-2 hover:text-ink">
              {more} more <span aria-hidden="true">→</span>
            </Link>
          ) : undefined}
        >
          <MergedToday rows={merged.slice(0, SHOWN_MERGES)} colours={projectColours(view.hosts)} />
        </Rows>
      </div>
      <div className="hidden min-w-0 min-[1200px]:block min-[1200px]:flex-[0_0_290px] min-[1600px]:sticky min-[1600px]:top-7 min-[1600px]:h-[calc(100vh-3.5rem)] min-[1600px]:flex-[0_0_330px] min-[1600px]:self-start">
        <DayLog entries={log.entries} />
      </div>
    </div>
  );
}
