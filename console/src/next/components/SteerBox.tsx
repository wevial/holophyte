import { useId, useState } from "react";
import { useAction } from "../actions";
import { Btn } from "./Btn";

const TOGGLE = "min-h-9 cursor-pointer px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.1em] text-ink-2 aria-pressed:bg-ink aria-pressed:text-paper";

/** The Steer box: a hint for `liveRun`, offered only while one is live, or
 *  an amendment to `ticketKey`, posted to `/actions/steer` at `base`. A taken
 *  note calls `onClose`; a refusal keeps the note and shows the daemon's detail. */
export function SteerBox({ ticketKey, base, liveRun, onClose }: { ticketKey: string; base: string; liveRun: number | null; onClose: () => void }) {
  const action = useAction();
  const [toRun, setToRun] = useState(false);
  const [note, setNote] = useState("");
  const ids = { label: useId(), group: useId(), line: useId() };
  const hint = toRun && liveRun != null;
  const refused = action.data != null && !action.data.ok ? action.data.detail : null;

  const send = async () => {
    const said = hint ? `Steer note sent to run ${liveRun}.` : `Amendment saved to ${ticketKey}.`;
    const result = await action.mutateAsync({ label: "Steer", base, route: "/actions/steer", body: { ticket: ticketKey, note, hint }, said });
    if (result.ok) onClose();
  };

  return (
    <section aria-labelledby={ids.label} data-steer-box className="mt-3.5 border-t border-rule bg-paper-2 px-5 pb-4 pt-3.5">
      <div id={ids.label} className="font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-2">
        Steer · amend this ticket
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <span id={ids.group} className="font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3">
          What the note changes
        </span>
        <div role="group" aria-labelledby={ids.group} className="inline-flex border border-ink-3">
          {liveRun != null && (
            <button type="button" aria-pressed={hint} onClick={() => setToRun(true)} className={TOGGLE}>
              This run
            </button>
          )}
          <button type="button" aria-pressed={!hint} onClick={() => setToRun(false)} className={TOGGLE}>
            The ticket
          </button>
        </div>
      </div>
      <textarea
        aria-label="Steer note"
        aria-describedby={ids.line}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        rows={3}
        placeholder={hint ? `Tell run ${liveRun} what to change…` : "Change the ticket: a criterion, a constraint, the scope…"}
        className="mt-3 block w-full max-w-[760px] resize-y border border-rule bg-paper px-3 py-2.5 font-plex text-[14px] leading-[1.5] text-ink placeholder:text-ink-3"
      />
      <p id={ids.line} className="m-0 mt-2 font-mono text-[12px] leading-[1.4] text-ink-3">
        {hint ? `Run ${liveRun} reads it on its next turn. The ticket stays as written.` : "Saved with the ticket. Every run from now on reads it."}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn variant="primary" disabled={note.trim() === ""} busy={action.isPending} busyLabel="Sending…" onClick={() => void send()}>
          {hint ? `Send to run ${liveRun}` : "Save amendment"}
        </Btn>
      </div>
      {refused != null && (
        <p role="alert" data-action-detail className="m-0 mt-3 max-w-[68ch] break-words font-mono text-[12.5px] leading-normal text-bad">
          {refused}
        </p>
      )}
    </section>
  );
}
