import { useState } from "react";
import type { RunResourceState } from "../hooks/useRunResource";
import { useTurnTranscript, type ChainTurns, type RunTurnsBody } from "../hooks/useRunTurns";
import { formatSettled, formatSpan } from "../lib/format";
import type { Fetch } from "../lib/poll";
import type { RunDetailBody } from "../lib/types";

type ChainRun = NonNullable<RunDetailBody["chain"]>["runs"][number];
/** Turn ids are unique only within their run, so a selection names both. */
type Selected = { run: number; turn: number };

/** The turns of run `id`'s chain; with more than one run, each run's turns
 *  under a header naming the run, its outcome (or phase while live) and its
 *  wall. A transcript opens through its own run. */
export function RunTurns({ base, id, chain, turns, deps }: {
  base: string; id: number; chain: ChainRun[]; turns: RunResourceState<ChainTurns>; deps?: { fetch: Fetch };
}) {
  const [selected, select] = useState<Selected | null>(null);
  const grouped = chain.length > 1;
  return <section aria-label="Turns" className="mt-4 rounded border border-line p-3">
    <h3 className="text-sm font-semibold">Turns</h3>
    {turns.loading && <p>Loading turns…</p>}
    {turns.error && <p role="alert">{turns.error}</p>}
    {turns.body?.map((group) => {
      const run = chain.find((entry) => entry.id === group.id);
      return <section key={group.id} data-turns-run={group.id} aria-label={grouped ? `run ${group.id} turns` : undefined}>
        {grouped && run && <h4 className="mt-2 text-[12px] font-semibold text-ink">
          Run {run.id} · {run.outcome ?? run.phase} · wall {(run.ended_ms == null ? formatSpan : formatSettled)(run.elapsed_ms)}
        </h4>}
        <TurnList base={base} run={group.id} turns={group.turns} selected={selected} select={select} panel={`transcript-${id}`} />
      </section>;
    })}
    {selected != null && <Transcript key={`${selected.run}/${selected.turn}`} base={base} selected={selected}
      panel={`transcript-${id}`} close={() => select(null)} deps={deps} />}
  </section>;
}

function TurnList({ base, run, turns, selected, select, panel }: {
  base: string; run: number; turns: RunTurnsBody["turns"]; selected: Selected | null;
  select: (selected: Selected) => void; panel: string;
}) {
  if (turns.length === 0) return <p>No recorded turns.</p>;
  return <ol className="mt-2 flex flex-col gap-2">
    {turns.map(turn => <li key={turn.id}>
      <span>{turn.role} · {turn.label ?? "label unknown"} · {turn.route} · {turn.seconds == null ? "duration unknown" : `${turn.seconds.toFixed(1)} s`} · {turn.session_id ?? "no session recorded"}</span>{" "}
      {turn.session_id && <a href={`${base}/runs/${run}/turns/${turn.id}/transcript`}
        aria-expanded={selected?.run === run && selected.turn === turn.id} aria-controls={panel}
        onClick={event => { event.preventDefault(); select({ run, turn: turn.id }); }}
        className="text-accent underline">Open transcript</a>}
    </li>)}
  </ol>;
}

function Transcript({ base, selected, panel, close, deps }: {
  base: string; selected: Selected; panel: string; close: () => void; deps?: { fetch: Fetch };
}) {
  const transcript = useTurnTranscript(base, selected.run, selected.turn, deps);
  return <section id={panel} aria-label="Transcript" className="mt-3 max-h-96 overflow-auto rounded border border-line p-3">
    <button type="button" onClick={close}>Close transcript</button>
    {transcript.loading && <p>Loading transcript…</p>}
    {transcript.error && <p role="alert">{transcript.error}</p>}
    {transcript.body?.entries.length === 0 && <p>No recognized transcript entries.</p>}
    {transcript.body?.entries.map((entry, index) => <div key={index} className="mt-3">
      <strong>{entry.speaker}</strong>
      <pre className="whitespace-pre-wrap break-words text-xs">{entry.text}</pre>
    </div>)}
  </section>;
}
