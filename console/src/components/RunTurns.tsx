import { useState } from "react";
import type { RunResourceState } from "../hooks/useRunResource";
import { useTurnTranscript, type RunTurnsBody } from "../hooks/useRunTurns";
import type { Fetch } from "../lib/poll";

export function RunTurns({ base, id, turns, deps }: {
  base: string; id: number; turns: RunResourceState<RunTurnsBody>; deps?: { fetch: Fetch };
}) {
  const [selected, select] = useState<number | null>(null);
  const transcript = useTurnTranscript(base, id, selected, deps);
  return <section aria-label="Turns" className="mt-4 rounded border border-line p-3">
    <h3 className="text-sm font-semibold">Turns</h3>
    {turns.loading && <p>Loading turns…</p>}
    {turns.error && <p role="alert">{turns.error}</p>}
    {turns.body?.turns.length === 0 && <p>No recorded turns.</p>}
    <ol className="mt-2 flex flex-col gap-2">
      {turns.body?.turns.map(turn => <li key={turn.id}>
        <span>{turn.role} · {turn.label ?? "label unknown"} · {turn.route} · {turn.seconds == null ? "duration unknown" : `${turn.seconds.toFixed(1)} s`} · {turn.session_id ?? "no session recorded"}</span>{" "}
        {turn.session_id && <a href={`${base}/runs/${id}/turns/${turn.id}/transcript`}
          aria-expanded={selected === turn.id} aria-controls={`transcript-${id}`}
          onClick={event => { event.preventDefault(); select(turn.id); }}
          className="text-accent underline">Open transcript</a>}
      </li>)}
    </ol>
    {selected != null && <section id={`transcript-${id}`} aria-label="Transcript" className="mt-3 max-h-96 overflow-auto rounded border border-line p-3">
      <button type="button" onClick={() => select(null)}>Close transcript</button>
      {transcript.loading && <p>Loading transcript…</p>}
      {transcript.error && <p role="alert">{transcript.error}</p>}
      {transcript.body?.entries.length === 0 && <p>No recognized transcript entries.</p>}
      {transcript.body?.entries.map((entry, index) => <div key={index} className="mt-3">
        <strong>{entry.speaker}</strong>
        <pre className="whitespace-pre-wrap break-words text-xs">{entry.text}</pre>
      </div>)}
    </section>}
  </section>;
}
