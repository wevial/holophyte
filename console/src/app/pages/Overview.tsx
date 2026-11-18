import { useContext, useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import { Link } from "@tanstack/react-router";
import { projectColours } from "../dayLog";
import { DayLog } from "../components/DayLog";
import { FocusCard, useOpenEntry } from "../components/FocusCard";
import { MergedToday, SHOWN_MERGES } from "../components/MergedToday";
import { FirstLoad, Notices } from "../components/Notices";
import { PullRequestGlyph, waitingRows, WaitingRows } from "../components/PrLane";
import { laneGroups, liveRuns, RunningRows, WorkerDots, witnessLines } from "../components/RunLane";
import { Section } from "../components/Section";
import { STATE_ICONS, TONE_TEXT } from "../components/StateDot";
import { WeekBars } from "../components/WeekBars";
import { firstLoadLine, noticeLines, silentHosts, staleConsole, tokenWanted } from "../notices";
import { useOverview } from "../overview";
import { useDayLedger, useMergedWeek, usePeerList } from "../queries";
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

function useHeightVariable(source: RefObject<HTMLElement | null>, target: RefObject<HTMLElement | null>, variable: string) {
  useLayoutEffect(() => {
    const from = source.current;
    const to = target.current;
    if (from == null || to == null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) to.style.setProperty(variable, `${entry.borderBoxSize[0]!.blockSize}px`);
    });
    observer.observe(from);
    return () => observer.disconnect();
  }, [source, target, variable]);
}

const COLUMNS = [
  "grid grid-cols-[minmax(0,780px)]",
  "min-[1200px]:grid-cols-[minmax(0,780px)_290px] min-[1200px]:grid-rows-[auto_auto_1fr] min-[1200px]:gap-x-[46px]",
  "min-[1600px]:grid-cols-[minmax(0,780px)_330px]",
  "min-[1680px]:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_380px] min-[1680px]:grid-rows-none min-[1680px]:gap-x-12",
].join(" ");

export function Overview() {
  const view = useOverview();
  const deps = useContext(PollDepsContext);
  const log = useDayLedger(view.hosts, view.now, deps);
  const week = useMergedWeek(view.hosts, view.now, deps.fetch);
  const colours = projectColours(view.hosts);
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
  const weekBars = useRef<HTMLDivElement>(null);
  const logColumn = useRef<HTMLDivElement>(null);
  useHeightVariable(weekBars, logColumn, "--week-bars-height");
  return (
    <div className="pt-3.5 pr-8 pb-7 pl-8 min-[1200px]:pl-[54px]">
      <div className={COLUMNS}>
        <div data-overview-column="now" className="col-start-1 row-start-1 flex min-w-0 flex-col gap-10">
          <div className="flex min-w-0 max-w-[780px] flex-col gap-6">
            {view.answered && (
              <header data-overview-head className="flex flex-col gap-3 pt-4 pl-2.5">
                <div className="flex flex-wrap items-baseline gap-x-8 gap-y-3">
                  <h1 className="flex items-baseline gap-[14px] font-sans text-ink">
                    <span data-numeral className={`text-[64px] font-[220] font-stretch-125% leading-[0.9] tracking-[-0.03em] ${need > 0 ? "text-need" : "text-act"}`}>{need > 0 ? need : running.length}</span>{" "}
                    <span className="text-[22px] font-normal font-stretch-112% leading-none tracking-[-0.01em]">{need > 0 ? "need you" : "running"}</span>
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
            <Section label="Needs you" count={need} empty="">
              <FocusCard entries={view.needs} open={opened.open} toggle={opened.toggle} hosts={view.hosts} now={view.now} />
            </Section>
          )}
          <Section
            label="Running"
            count={shown(running.length)}
            aside={loaded && lanes.length > 0 ? <WorkerDots groups={lanes} /> : undefined}
            empty="Nothing running."
            footer={witnesses.map((line) => (
              <p key={line} data-witnesses className="px-2.5 pt-2.5 font-mono text-[12px] text-ink-3">{line}</p>
            ))}
          >
            <RunningRows rows={running} />
          </Section>
          <Section label="Waiting" count={shown(waiting.length)} empty="Nothing waits on a pull request.">
            <WaitingRows rows={waiting} readiness={view.readiness} />
          </Section>
        </div>
        <div data-overview-column="landed" className="contents min-[1680px]:col-start-2 min-[1680px]:row-start-1 min-[1680px]:flex min-[1680px]:min-w-0 min-[1680px]:flex-col min-[1680px]:gap-10">
          <div className="col-start-1 row-start-2 mt-10 min-w-0 min-[1680px]:mt-0">
            <Section
              label="Merged today"
              count={shown(merged.length)}
              empty="Nothing merged yet today."
              aside={more > 0 ? (
                <Link to="/runs" search={(previous: ScopeSearch) => ({ ...keepScope(previous), state: "merged", range: "today" })} className="inline-flex items-center gap-1.5 font-sans text-[13px] text-ink-2 hover:text-ink">
                  {more} more <span aria-hidden="true">→</span>
                </Link>
              ) : undefined}
            >
              <MergedToday rows={merged.slice(0, SHOWN_MERGES)} colours={colours} />
            </Section>
          </div>
          <div ref={weekBars} className="relative z-20 col-start-2 row-span-full hidden min-w-0 self-start min-[1200px]:block min-[1600px]:sticky min-[1600px]:top-3.5 min-[1680px]:static min-[1680px]:z-auto min-[1680px]:self-auto">
            <WeekBars rows={week} now={view.now} colours={colours} />
          </div>
        </div>
        <div ref={logColumn} data-overview-column="log" className="col-start-2 row-span-full hidden min-w-0 flex-col self-start pt-[calc(var(--week-bars-height,0px)+2rem)] min-[1200px]:flex min-[1600px]:sticky min-[1600px]:top-3.5 min-[1600px]:h-[max(calc(var(--panel-height)-2.625rem),calc(var(--week-bars-height,0px)+14rem))] min-[1680px]:col-start-3 min-[1680px]:row-auto min-[1680px]:pt-0">
          <DayLog entries={log.entries} className="min-h-0 flex-1" />
        </div>
      </div>
    </div>
  );
}
