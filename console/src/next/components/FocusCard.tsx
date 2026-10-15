import { useContext, useState, type Ref } from "react";
import { ageOf, describe, entryKey, prFacts, type NeedsYouEntry, type PrFacts } from "../../lib/attention";
import type { Triage } from "../../lib/contract.gen";
import { formatAge, formatClock, formatSettled } from "../../lib/format";
import { sinceSeen, UNREACHABLE, type HostRecord } from "../../lib/hosts";
import { agentMs, latestEvents, workingMs } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { useRun } from "../queries";
import { PollDepsContext } from "../scope";
import { STATES, stateOf } from "../state";
import { Btn } from "./Btn";
import { CardActions } from "./CardActions";
import { ChevronIcon } from "./icons";
import { StateDot } from "./StateDot";

type Host = HostRecord<ContractBodies>;

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const str = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

/** The card's entry: the held key where it still stands, else whatever
 *  now stands at its last position, else the last entry. */
export function useHeldEntry(entries: NeedsYouEntry[]): { index: number; hold: (index: number) => void } {
  const [held, setHeld] = useState<{ key: string; index: number } | null>(null);
  const keys = entries.map(entryKey);
  const found = held == null ? -1 : keys.indexOf(held.key);
  const index = found >= 0 ? found : Math.max(0, Math.min(held?.index ?? 0, entries.length - 1));
  const hold = (next: number) => setHeld(keys[next] == null ? null : { key: keys[next], index: next });
  if (keys.length > 0 && (held?.key !== keys[index] || held?.index !== index)) hold(index);
  return { index, hold };
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
function describeOn(item: AttentionItem, host: Host | undefined, now: number) {
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
  const pr = item.kind === "pr_open" && item.pr != null && typeof item.pr === "object" ? (item.pr as PrFacts) : null;
  const number = num(pr?.number);
  const label = number != null ? `PR #${number}` : url != null ? prLabel(url) : null;
  if (label == null) return <p className="text-ink-3">No pull request yet</p>;
  const name = url == null ? label : <a href={url} target="_blank" rel="noreferrer" className={linkClass}>{label}</a>;
  return (
    <p data-pr-line>
      {name}
      {pr != null && ` · ${prFacts(pr).map((fact) => fact.label).join(" · ")}`}
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
          <span className="break-words">{event.summary}</span>
        </li>
      ))}
    </ol>
  );
}

function stateOfEntry(entry: NeedsYouEntry) {
  return stateOf({ attention: entry.item, readiness: { ready: entry.readyToMerge } });
}

function Pager({ index, count, hold }: { index: number; count: number; hold: (index: number) => void }) {
  const pagerButton = "grid size-11 place-items-center border border-rule bg-paper text-ink-2 hover:text-ink disabled:opacity-40";
  return (
    <span data-pager className="flex w-full items-center gap-2 min-[720px]:ml-auto min-[720px]:w-auto">
      <span>
        {index + 1} of {count}
      </span>
      <button type="button" aria-label="Previous item" disabled={index === 0} onClick={() => hold(index - 1)} className={pagerButton}>
        <ChevronIcon folded={false} />
      </button>
      <button type="button" aria-label="Next item" disabled={index === count - 1} onClick={() => hold(index + 1)} className={pagerButton}>
        <ChevronIcon folded />
      </button>
    </span>
  );
}

function Card({
  entry,
  index,
  count,
  hosts,
  now,
  hold,
  cardRef,
}: {
  entry: NeedsYouEntry;
  index: number;
  count: number;
  hosts: Host[];
  now: number;
  hold: (index: number) => void;
  cardRef?: Ref<HTMLElement>;
}) {
  const deps = useContext(PollDepsContext);
  const { item } = entry;
  const host = hostOf(hosts, item);
  const unreachable = item.kind === UNREACHABLE;
  const runId = unreachable ? null : num(item.run);
  const run = useRun(runId == null ? null : (host?.base ?? null), runId, deps);
  const description = describeOn(item, host, now);
  const key = str(item.ticket);
  const ticketUrl = str(item.ticket_url);
  const age = ageOf(item, now);
  const seen = num(item.last_seen_ms);
  const title = unreachable ? description.body : (namedTitle(item, host) ?? str(run.data?.run.title) ?? key);
  return (
    <article ref={cardRef} tabIndex={-1} aria-label="Focus" data-focus={key ?? undefined} className="flex flex-col border border-rule bg-paper px-7 pb-6 pt-[26px] max-[720px]:px-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[12.5px] text-ink-3">
        <StateDot state={stateOfEntry(entry)} />
        {key != null &&
          (ticketUrl == null ? (
            <span className="text-ink-2">{key}</span>
          ) : (
            <a href={ticketUrl} target="_blank" rel="noreferrer" className={linkClass}>
              {key}
            </a>
          ))}
        {age != null && <span>waiting {formatSettled(age)}</span>}
        <Pager index={index} count={count} hold={hold} />
      </div>
      {title != null && <h3 className="mt-3.5 break-words font-sans text-[20px] font-semibold leading-tight text-ink">{title}</h3>}
      {unreachable ? (
        <p className="mt-1.5 font-mono text-[12px] text-ink-3">{seen == null ? "never answered" : `last seen ${formatAge(now - seen)} ago`}</p>
      ) : (
        <>
          {description.body !== "" && <p className="mt-2.5 max-w-[68ch] break-words text-[16px] leading-normal text-ink-2">{whyOf(description.body, item)}</p>}
          {description.meta != null && <p className="mt-1.5 font-mono text-[12px] text-ink-3">{description.meta}</p>}
          <div className="mt-5 flex flex-col gap-3.5 border-t border-dashed border-rule pt-[18px] font-mono text-[12.5px] leading-normal text-ink-2">
            <PullRequestLine item={item} />
            {runId != null && <LatestEvents run={run} />}
          </div>
          <CardActions entry={entry} host={host} actions={description.actions} title={title} />
        </>
      )}
    </article>
  );
}

function Row({ entry, hosts, now, onPick }: { entry: NeedsYouEntry; hosts: Host[]; now: number; onPick: () => void }) {
  const { item } = entry;
  const state = stateOfEntry(entry);
  const name = str(item.ticket) ?? str(item.host) ?? str(item.daemon) ?? item.kind;
  const title = item.kind === UNREACHABLE ? null : namedTitle(item, hostOf(hosts, item));
  const age = ageOf(item, now);
  return (
    <li>
      <button
        type="button"
        aria-label={`Show ${name}`}
        onClick={onPick}
        className="grid min-h-[50px] w-full grid-cols-[20px_88px_minmax(0,1fr)_auto] items-center gap-3 px-4 py-2 text-left font-mono text-[12px] text-ink-3 hover:bg-paper-2 max-[720px]:grid-cols-[20px_minmax(0,1fr)_auto]"
      >
        <StateDot state={state} hideWord />
        <span className="truncate text-ink-2">{name}</span>
        <span className="truncate font-sans text-[14px] font-medium text-ink max-[720px]:hidden">{title}</span>
        <span>
          {STATES[state].word}
          {age != null && ` · ${formatAge(age)}`}
        </span>
      </button>
    </li>
  );
}

/** The Needs you queue: the held entry as the card, every other as a row
 *  that becomes the card when pressed. */
export function FocusCard({
  entries,
  index,
  hold,
  hosts,
  now,
  cardRef,
}: {
  entries: NeedsYouEntry[];
  index: number;
  hold: (index: number) => void;
  hosts: Host[];
  now: number;
  cardRef?: Ref<HTMLElement>;
}) {
  const entry = entries[index];
  if (entry == null) return null;
  const last = index === entries.length - 1;
  return (
    <div className="flex flex-col gap-2.5">
      <Card key={entryKey(entry, index)} entry={entry} index={index} count={entries.length} hosts={hosts} now={now} hold={hold} cardRef={cardRef} />
      {!last && (
        <div className="flex justify-end">
          <Btn onClick={() => hold(index + 1)}>Later, show the next one</Btn>
        </div>
      )}
      {entries.length > 1 && (
        <ul aria-label="Then" className="divide-y divide-rule-2 border border-rule bg-paper">
          {entries.map((other, at) =>
            at === index ? null : <Row key={entryKey(other, at)} entry={other} hosts={hosts} now={now} onPick={() => hold(at)} />,
          )}
        </ul>
      )}
    </div>
  );
}
