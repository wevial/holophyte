import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { useEffect, useRef, useState } from "react";
import { requeueBody } from "../../lib/actions";
import { Btn } from "./Btn";
import { detailLine } from "./CardActions";

const REQUEUE_NOTE = "Note";
const REVIEWERS_SEE = "Reviewers see this";
const POPUPS = '[role="dialog"], [role="listbox"], [role="menu"], [role="combobox"]';

export function RequeueForm({ ticket, busy, refused, onRequeue, onCancel }: {
  ticket: string;
  busy: boolean;
  refused: string | null;
  onRequeue: (body: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const [amend, setAmend] = useState(false);
  const cancel = useRef(onCancel);
  cancel.current = onCancel;
  const pending = useRef(busy);
  pending.current = busy;
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // On body so a widget's own Escape runs first, and before the peek's document listener.
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || pending.current || document.querySelector('[aria-modal="true"]')) return;
      const popup = event.target instanceof Element ? event.target.closest(POPUPS) : null;
      if (popup != null && !popup.contains(box.current)) return;
      event.preventDefault();
      cancel.current();
    };
    document.body.addEventListener("keydown", escape);
    return () => document.body.removeEventListener("keydown", escape);
  }, []);
  return (
    <div ref={box} role="group" aria-label={`Requeue ${ticket}`} data-requeue-form className="flex w-full max-w-[520px] flex-col gap-3">
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
