import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { useState, type KeyboardEvent } from "react";
import { requeueBody } from "../../lib/actions";
import { Btn } from "./Btn";
import { detailLine } from "./CardActions";

const REQUEUE_NOTE = "Note";
const REVIEWERS_SEE = "Reviewers see this";

/** Requeue's form for `ticket`: a note, a switch that sends it to the
 *  reviewers as an amendment, and Requeue and Cancel. Requeue hands
 *  `onRequeue` the body to post; Cancel or Escape calls `onCancel`. The
 *  daemon's `refused` detail reads inside the form. */
export function RequeueForm({ ticket, busy, refused, onRequeue, onCancel }: {
  ticket: string;
  busy: boolean;
  refused: string | null;
  onRequeue: (body: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const [amend, setAmend] = useState(false);
  const escape = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || busy) return;
    event.preventDefault();
    event.stopPropagation();
    onCancel();
  };
  return (
    <div role="group" aria-label={`Requeue ${ticket}`} data-requeue-form onKeyDown={escape} className="flex w-full max-w-[520px] flex-col gap-3">
      <textarea
        aria-label={REQUEUE_NOTE}
        autoFocus
        rows={2}
        value={text}
        onChange={(event) => setText(event.target.value)}
        className="field block w-full resize-y font-sans"
      />
      <label className="flex min-h-8 items-center gap-2.5 text-[13px] text-ink-2">
        <Switch aria-label={REVIEWERS_SEE} checked={amend} onCheckedChange={setAmend} className="switch">
          <SwitchThumb />
        </Switch>
        <span aria-hidden="true">{REVIEWERS_SEE}</span>
      </label>
      <div className="flex flex-wrap gap-2">
        <Btn variant="primary" busy={busy} busyLabel="Requeuing…" onClick={() => onRequeue(requeueBody(ticket, text, amend))}>
          Requeue
        </Btn>
        <Btn disabled={busy} onClick={onCancel}>Cancel</Btn>
      </div>
      {refused != null && (
        <p role="status" data-action-detail className={`m-0 text-bad ${detailLine}`}>
          {refused}
        </p>
      )}
    </div>
  );
}
