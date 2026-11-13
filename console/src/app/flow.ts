import type { Flow, FlowHours } from "../lib/contract.gen";

export const WAIT_WORDS: Record<string, string> = {
  pending_checks: "Parked for PR checks",
  checks_failing: "Checks still failing after the rerun",
  thread: "A review thread needs an answer",
  rounds_used: "Review rounds used up",
  behind_main: "Behind main: update, then CI again",
  main_red: "Main red, merges held",
  approval: "Waiting for approval to merge",
  other: "Other waits",
};

export const BY_DESIGN = new Set(["approval"]);

const BIGGEST_WAITS = 6;
const COUNT_WORDS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve"];

export interface FlowNumbers {
  merged: number;
  hours: FlowHours;
  total: number;
  waitingShare: number | null;
  rounds: { low: number; high: number } | null;
  interventionsPerMerge: number | null;
  reverted: { count: number; merged: number } | null;
  waits: { group: string; hours: number }[];
  phases: { phase: string; hours: number }[];
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2;
}

/** The median of a flow's weekly round medians, its weeks without a merge left out. */
function flowRounds(flow: Flow): number | null {
  return median(flow.weeks.flatMap((week) => (week.rounds_median == null ? [] : [week.rounds_median])));
}

function addInto(into: Record<string, number>, from: Record<string, unknown>) {
  for (const [key, value] of Object.entries(from)) if (typeof value === "number") into[key] = (into[key] ?? 0) + value;
}

/** `flows` summed: counts and hours add, waiting share and per-merge ratios
 *  are taken over the sums, and rounds span each flow's own median. */
export function flowNumbers(flows: Flow[]): FlowNumbers {
  const hours: FlowHours = { working: 0, waiting: 0, operator: 0 };
  const waits: Record<string, number> = {};
  const phases: Record<string, number> = {};
  let merged = 0;
  let interventions = 0;
  let revertedCount = 0;
  let revertedMerged = 0;
  let revertedKnown = flows.length > 0;
  for (const flow of flows) {
    for (const week of flow.weeks) {
      merged += week.merged;
      interventions += week.interventions;
      hours.working += week.hours.working;
      hours.waiting += week.hours.waiting;
      hours.operator += week.hours.operator;
    }
    addInto(waits, flow.waits);
    addInto(phases, flow.phases);
    if (flow.reverted == null) revertedKnown = false;
    else {
      revertedCount += flow.reverted.count;
      revertedMerged += flow.reverted.merged;
    }
  }
  const total = hours.working + hours.waiting + hours.operator;
  const medians = flows.map(flowRounds).filter((value) => value != null);
  const byHours = (a: { hours: number }, b: { hours: number }) => b.hours - a.hours;
  return {
    merged,
    hours,
    total,
    waitingShare: total > 0 ? Math.round((100 * (hours.waiting + hours.operator)) / total) : null,
    rounds: medians.length > 0 ? { low: Math.min(...medians), high: Math.max(...medians) } : null,
    interventionsPerMerge: merged > 0 ? interventions / merged : null,
    reverted: revertedKnown ? { count: revertedCount, merged: revertedMerged } : null,
    waits: Object.entries(waits)
      .map(([group, value]) => ({ group, hours: value }))
      .filter((wait) => wait.hours > 0)
      .sort(byHours)
      .slice(0, BIGGEST_WAITS),
    phases: Object.entries(phases)
      .map(([phase, value]) => ({ phase, hours: value }))
      .sort(byHours),
  };
}

const DAY_MONTH = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long" });

/** `Six weeks, 2 October to 12 November` over the widest of `flows`. */
export function flowSpan(flows: Flow[]): string | null {
  const starts = flows.flatMap((flow) => (flow.weeks[0] ? [flow.weeks[0].start_ms] : []));
  if (starts.length === 0) return null;
  const weeks = Math.max(...flows.map((flow) => flow.weeks.length));
  const count = COUNT_WORDS[weeks] ?? String(weeks);
  const end = Math.max(...flows.map((flow) => flow.now));
  return `${count} ${weeks === 1 ? "week" : "weeks"}, ${DAY_MONTH.format(Math.min(...starts))} to ${DAY_MONTH.format(end)}`;
}

function oneDecimal(value: number): string {
  return String(Math.round(value * 10) / 10);
}

export function formatHours(hours: number): string {
  return `${hours >= 10 ? Math.round(hours) : oneDecimal(hours)} h`;
}

export function formatRounds(rounds: FlowNumbers["rounds"]): string {
  if (rounds == null) return "–";
  return rounds.low === rounds.high ? oneDecimal(rounds.low) : `${oneDecimal(rounds.low)} to ${oneDecimal(rounds.high)}`;
}

export function formatPerMerge(value: number | null): string {
  return value == null ? "–" : oneDecimal(value);
}

export function formatReverted(reverted: FlowNumbers["reverted"]): string {
  return reverted == null ? "–" : `${reverted.count} of ${reverted.merged}`;
}
