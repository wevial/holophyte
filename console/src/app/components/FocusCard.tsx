import { useContext, useEffect, useId, useState } from "react";
import { ageOf, describe, entryKey, type NeedsYouEntry } from "../../lib/attention";
import type { Triage } from "../../lib/contract.gen";
import { formatAge } from "../../lib/format";
import { sinceSeen, UNREACHABLE, type HostRecord } from "../../lib/hosts";
import { agentMs, workingMs } from "../../lib/runs";
import type { AttentionItem } from "../../lib/types";
import { blockedAsk, blockedHeadline } from "../focus";
import { LimitedMarkdown } from "./LimitedMarkdown";
import type { ContractBodies } from "../fleet";
import { useRun } from "../queries";
import { PollDepsContext } from "../scope";
import { stateOf } from "../state";
import { CardActions } from "./CardActions";
import { StateDot } from "./StateDot";
import { TitleText } from "./TitleText";

type Host = HostRecord<ContractBodies>;

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const str = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

/** The open row's index, or null once the open row is closed: the held key
 *  where it still stands, else the oldest row. */
export function useOpenEntry(entries: NeedsYouEntry[]): { open: number | null; toggle: (index: number) => void } {
  const [held, setHeld] = useState<string | null | undefined>(undefined);
  const keys = entries.map(entryKey);
  const found = typeof held === "string" ? keys.indexOf(held) : -1;
  const open = held === null || keys.length === 0 ? null : Math.max(found, 0);
  if (open != null && held !== keys[open]) setHeld(keys[open]);
  const toggle = (index: number) => setHeld(index === open ? null : keys[index]);
  return { open, toggle };
}

/** The title the item names, else the one its live run carries. */
function namedTitle(item: AttentionItem, host: Host | undefined): string | null {
  const run = num(item.run);
  const live = run == null ? undefined : host?.status?.runs.find((candidate) => candidate.id === run);
  return str(item.title) ?? str(live?.title);
}

/** `describe()` against the item's own daemon's thresholds and clock, aged
 *  by the local time since that daemon last answered. */
export function describeOn(item: AttentionItem, host: Host | undefined, now: number) {
  const status = host?.status;
  if (item.kind === UNREACHABLE || !status) return describe(item, { heartbeat_stale_ms: 0, strikes: 0 }, { now });
  const since = sinceSeen(host?.seen_ms, now);
  const aged = since && typeof item.heartbeat_age_ms === "number" ? { ...item, heartbeat_age_ms: item.heartbeat_age_ms + since } : item;
  const runs = since
    ? status.runs.map((run) => ({ ...run, elapsed_ms: run.elapsed_ms + since, working_ms: workingMs(run, since), agent_ms: run.agent_ms == null ? run.agent_ms : agentMs(run, since) }))
    : status.runs;
  return describe(aged, status.thresholds, { now: status.now + since, runs });
}

const TRIAGE_CHOICE: Record<string, string> = { code: "the code", infra: "the host or a tool", spec: "the ticket" };

function triageSentence(item: AttentionItem): string | null {
  if (item.kind !== "failed" || item.triage == null || typeof item.triage !== "object") return null;
  const triage = item.triage as Partial<Triage>;
  const choice = triage.choice == null ? undefined : TRIAGE_CHOICE[triage.choice];
  const confidence = num(triage.confidence);
  if (choice == null || confidence == null) return null;
  return `Triage says it is ${choice}, ${Math.round(confidence * 100)}%.`;
}

function whyOf(body: string, item: AttentionItem): string {
  const triage = triageSentence(item);
  return triage == null ? body : `${body}. ${triage}`;
}

function Detail({ entry, host, now, title, id }: { entry: NeedsYouEntry; host: Host | undefined; now: number; title: string | null; id: string }) {
  const { item } = entry;
  const description = describeOn(item, host, now);
  const seen = num(item.last_seen_ms);
  const sentence = "max-w-[75ch] break-words font-read text-[16px] leading-[1.5] text-ink";
  return (
    <div id={id} data-need-detail className="flex flex-col gap-3 pt-1 pb-4 pl-12 pr-4 max-[720px]:px-3 [&>[data-card-actions]]:mt-0">
      {item.kind === UNREACHABLE ? (
        <p className={sentence}>{description.body} {seen == null ? "It never answered." : `Last seen ${formatAge(now - seen)} ago.`}</p>
      ) : item.kind === "blocked" ? (
        <div className={`${sentence} [&_button]:font-sans`}>
          <LimitedMarkdown lines={2}>{item.park_kind === "fix_declined" ? `${blockedHeadline(item)}. ${blockedAsk(item)}` : blockedAsk(item)}</LimitedMarkdown>
        </div>
      ) : (
        description.body !== "" && <p className={sentence}>{whyOf(description.body, item)}</p>
      )}
      {item.kind !== UNREACHABLE && <CardActions entry={entry} host={host} actions={description.actions} title={title} />}
    </div>
  );
}

const EXPAND_MS = 200;

/** `open` as it stood `EXPAND_MS` ago, so a row keeps its detail through
 *  its collapse and clips it only while it expands. */
function useSettled(open: boolean): boolean {
  const [settled, setSettled] = useState(open);
  useEffect(() => {
    if (open === settled) return;
    const timer = setTimeout(() => setSettled(open), EXPAND_MS);
    return () => clearTimeout(timer);
  }, [open, settled]);
  return settled;
}

function WaitRow({ entry, hosts, now, open, onToggle }: { entry: NeedsYouEntry; hosts: Host[]; now: number; open: boolean; onToggle: () => void }) {
  const deps = useContext(PollDepsContext);
  const detailId = useId();
  const { item } = entry;
  const host = hosts.find((candidate) => candidate.key === item.daemon);
  const unreachable = item.kind === UNREACHABLE;
  const runId = unreachable ? null : num(item.run);
  const run = useRun(open && runId != null ? (host?.base ?? null) : null, runId, deps);
  const state = stateOf({ attention: item, readiness: { ready: entry.readyToMerge } });
  const key = str(item.ticket);
  const name = key ?? str(item.host) ?? str(item.daemon) ?? item.kind;
  const title = unreachable ? null : (namedTitle(item, host) ?? str(run.data?.run.title));
  const age = ageOf(item, now);
  const settled = useSettled(open);
  return (
    <li
      data-wait-row={name}
      data-open={open || undefined}
      className="group border-b border-rule-2 data-open:rounded-lg data-open:border-b-0 data-open:bg-(image:--need-tint) data-open:shadow-[inset_0_0_0_1px_var(--need-edge)]"
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={onToggle}
        className="grid min-h-8 w-full grid-cols-[16px_84px_minmax(0,1fr)_auto] items-center gap-3 px-2.5 py-1 text-left font-mono text-[12px] text-ink-3 hover:bg-paper-2 group-data-open:rounded-t-lg max-[720px]:grid-cols-[16px_minmax(0,1fr)_auto]"
      >
        <StateDot state={state} hideWord />
        <span className="truncate text-ink-2">{name}</span>
        <span className="truncate font-read text-[14.5px] font-medium leading-[1.35] text-ink max-[720px]:hidden">{title != null && <TitleText>{title}</TitleText>}</span>
        <span data-age>{age != null && formatAge(age)}</span>
      </button>
      <div
        data-need-panel
        inert={!open}
        className="grid grid-rows-[0fr] opacity-0 transition-[grid-template-rows,opacity] duration-200 ease-out group-data-open:grid-rows-[1fr] group-data-open:opacity-100 motion-reduce:transition-none"
      >
        <div className={`min-h-0 ${open && settled ? "" : "overflow-hidden"}`}>{(open || settled) && <Detail entry={entry} host={host} now={now} title={title ?? key} id={detailId} />}</div>
      </div>
    </li>
  );
}

/** Needs you: a row each, oldest first, the open one showing what the
 *  entry asks and the actions that answer it. */
export function FocusCard({
  entries,
  open,
  toggle,
  hosts,
  now,
}: {
  entries: NeedsYouEntry[];
  open: number | null;
  toggle: (index: number) => void;
  hosts: Host[];
  now: number;
}) {
  return (
    <ul aria-label="Needs you">
      {entries.map((entry, at) => (
        <WaitRow key={entryKey(entry, at)} entry={entry} hosts={hosts} now={now} open={at === open} onToggle={() => toggle(at)} />
      ))}
    </ul>
  );
}
