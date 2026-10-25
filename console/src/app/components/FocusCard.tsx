import { useContext, useId, useState } from "react";
import { ageOf, describe, entryKey, type NeedsYouEntry } from "../../lib/attention";
import type { Triage } from "../../lib/contract.gen";
import { formatAge, formatClock } from "../../lib/format";
import { sinceSeen, UNREACHABLE, type HostRecord } from "../../lib/hosts";
import { agentMs, latestEvents, workingMs } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import { blockedAsk, blockedHeadline, focusPrLabel } from "../focus";
import { LimitedMarkdown } from "./LimitedMarkdown";
import type { ContractBodies } from "../fleet";
import { useRun } from "../queries";
import { PollDepsContext } from "../scope";
import { STATES, stateOf } from "../state";
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

const hostOf = (hosts: Host[], item: AttentionItem) => hosts.find((host) => host.key === item.daemon);

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

const linkClass = "border-b border-rule text-ink-2 hover:border-ink hover:text-ink";

function PullRequestLine({ item }: { item: AttentionItem }) {
  const url = str(item.pr_url);
  if (url == null) return <p className="text-ink-3">No pull request yet</p>;
  return (
    <p data-pr-line>
      <a href={url} target="_blank" rel="noreferrer" className={linkClass}>{prLabel(url)}</a>
    </p>
  );
}

function LatestEvents({ run }: { run: ReturnType<typeof useRun> }) {
  if (run.isPending) return <p className="text-ink-3">Reading the run…</p>;
  if (run.isError) return <p className="text-bad">The run could not be read: {run.error.message}</p>;
  const events = latestEvents(run.data.events, 3);
  if (events.length === 0) return null;
  return (
    <ol aria-label="Latest progress" className="flex flex-col">
      {events.map((event, index) => (
        <li key={`${event.at}-${index}`} className="grid grid-cols-[44px_minmax(0,1fr)] gap-2.5 py-0.5">
          <time className="text-ink-3">{formatClock(event.at)}</time>
          <LimitedMarkdown lines={2}>{event.summary}</LimitedMarkdown>
        </li>
      ))}
    </ol>
  );
}

function stateOfEntry(entry: NeedsYouEntry) {
  return stateOf({ attention: entry.item, readiness: { ready: entry.readyToMerge } });
}

function Detail({ entry, host, now, run, title, id }: { entry: NeedsYouEntry; host: Host | undefined; now: number; run: ReturnType<typeof useRun>; title: string | null; id: string }) {
  const { item } = entry;
  const description = describeOn(item, host, now);
  const seen = num(item.last_seen_ms);
  const detailClass = "border-t border-dashed border-rule-2 py-4 pl-[148px] pr-5 max-[720px]:px-4";
  if (item.kind === UNREACHABLE) {
    return (
      <div id={id} className={detailClass}>
        <p className="font-read text-[16px] leading-[1.5] text-ink-2">{description.body}</p>
        <p className="mt-1.5 font-mono text-[12px] text-ink-3">{seen == null ? "never answered" : `last seen ${formatAge(now - seen)} ago`}</p>
      </div>
    );
  }
  return (
    <div id={id} className={detailClass}>
      {item.kind === "blocked" ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-[16px] font-semibold text-ink">{blockedHeadline(item)}</h4>
            {str(item.pr_url) && <a href={str(item.pr_url)!} target="_blank" rel="noreferrer" className="rounded border border-rule px-2 py-0.5 font-mono text-xs text-ink-2 hover:text-ink">{focusPrLabel(item)}</a>}
          </div>
          <div className="mt-2.5 max-w-[75ch] font-read text-[16px] leading-[1.5] text-ink-2 [&_button]:font-sans">
            <LimitedMarkdown lines={4}>{blockedAsk(item)}</LimitedMarkdown>
          </div>
        </>
      ) : description.body !== "" && <p className="max-w-[75ch] break-words font-read text-[16px] leading-[1.5] text-ink-2">{whyOf(description.body, item)}</p>}
      {description.meta != null && <p className="mt-1.5 font-mono text-[12px] text-ink-3">{description.meta}</p>}
      <div className="mt-4 flex flex-col gap-3.5 border-t border-dashed border-rule pt-4 font-mono text-[12.5px] leading-normal text-ink-2">
        {item.kind !== "blocked" && <PullRequestLine item={item} />}
        {num(item.run) != null && <LatestEvents run={run} />}
      </div>
      <CardActions entry={entry} host={host} actions={description.actions} title={title} />
    </div>
  );
}

function WaitRow({ entry, hosts, now, open, onToggle }: { entry: NeedsYouEntry; hosts: Host[]; now: number; open: boolean; onToggle: () => void }) {
  const deps = useContext(PollDepsContext);
  const detailId = useId();
  const { item } = entry;
  const host = hostOf(hosts, item);
  const unreachable = item.kind === UNREACHABLE;
  const runId = unreachable ? null : num(item.run);
  const run = useRun(open && runId != null ? (host?.base ?? null) : null, runId, deps);
  const state = stateOfEntry(entry);
  const key = str(item.ticket);
  const name = key ?? str(item.host) ?? str(item.daemon) ?? item.kind;
  const title = unreachable ? null : (namedTitle(item, host) ?? str(run.data?.run.title));
  const age = ageOf(item, now);
  const prUrl = str(item.pr_url);
  return (
    <li data-wait-row={name} data-open={open || undefined} className="group border-rule-2 [&+&]:border-t data-open:bg-need-wash data-open:shadow-[inset_2px_0_0_var(--need)]">
      <div className="flex items-center pr-4 hover:bg-paper-2 group-data-open:hover:bg-transparent">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={detailId}
          onClick={onToggle}
          className="grid min-h-[50px] min-w-0 flex-1 grid-cols-[20px_88px_minmax(0,1fr)_auto] items-center gap-3 py-2 pl-4 pr-3 text-left font-mono text-[12px] text-ink-3 max-[720px]:grid-cols-[20px_minmax(0,1fr)_auto]"
        >
          <StateDot state={state} hideWord />
          <span className="truncate text-ink-2">{name}</span>
          <span className="truncate font-read text-[15px] font-medium leading-[1.35] text-ink max-[720px]:hidden">{title != null && <TitleText>{title}</TitleText>}</span>
          <span>
            {STATES[state].word}
            {age != null && ` · ${formatAge(age)}`}
          </span>
        </button>
        {prUrl != null && (
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 whitespace-nowrap border-b border-rule font-mono text-[12px] text-ink hover:border-ink"
          >
            {prLabel(prUrl).replace(/^PR (?=#)/, "")}
          </a>
        )}
      </div>
      {open && <Detail entry={entry} host={host} now={now} run={run} title={title ?? key} id={detailId} />}
    </li>
  );
}

/** Waiting on you: a row each, oldest first, the open one showing what the
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
  if (entries.length === 0) return null;
  return (
    <ul aria-label="Waiting on you" className="border border-rule bg-paper">
      {entries.map((entry, at) => (
        <WaitRow key={entryKey(entry, at)} entry={entry} hosts={hosts} now={now} open={at === open} onToggle={() => toggle(at)} />
      ))}
    </ul>
  );
}
