import { useId, useRef, useState } from "react";
import { useAction } from "../actions";
import { Btn } from "./Btn";
import { ACTIONS_OFF_LINE, detailLine } from "./CardActions";
import { ConfirmDialog } from "./ConfirmDialog";

const STOP = "Stop";

/** Stop for a queued or live scout: a confirm that asks for a reason, then
 *  `POST /actions/scout-stop`; a refusal reads inside the confirm. */
export function ScoutStop({ base, project, scout, actions }: { base: string; project: string; scout: number; actions: boolean }) {
  const action = useAction();
  const [stopping, setStopping] = useState(false);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const opener = useRef<HTMLButtonElement>(null);
  const refused = action.data != null && !action.data.ok ? action.data.detail : null;

  const open = () => {
    action.reset();
    setReason("");
    setStopping(true);
  };
  const keepRunning = () => {
    setStopping(false);
    opener.current?.focus();
  };
  const confirm = async () => {
    const result = await action.mutateAsync({
      label: STOP, base, route: "/actions/scout-stop", body: { scout, note: reason.trim() }, said: `Stop sent to scout ${scout}.`,
    });
    if (result.ok) setStopping(false);
  };

  return (
    <div data-scout-stop className="flex shrink-0 flex-col items-end gap-2">
      <Btn ref={opener} variant="danger" disabled={!actions} title={actions ? undefined : ACTIONS_OFF_LINE} onClick={open}>
        {STOP}
      </Btn>
      {stopping && (
        <ConfirmDialog
          label={`Stop · ${project}`}
          heading={`Stop scout ${scout}?`}
          confirm="Stop scout"
          busyLabel="Stopping…"
          busy={action.isPending}
          cancelLabel="Keep running"
          confirmVariant="danger-fill"
          confirmDisabled={reason.trim() === ""}
          onConfirm={() => void confirm()}
          onCancel={keepRunning}
          refusal={
            refused != null && (
              <p role="alert" className={`mt-3 text-bad ${detailLine}`}>
                {refused}
              </p>
            )
          }
        >
          <p className="m-0 mt-3 font-sans text-[14px] leading-[1.5] text-ink-2">Its lead and helpers stop now, and it writes no report.</p>
          <label htmlFor={reasonId} className="mt-4 block font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">
            Reason · saved with the scout
          </label>
          <textarea
            id={reasonId}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={3}
            placeholder="Why stop it?"
            className="mt-2 block w-full resize-y border border-rule bg-paper px-3 py-2.5 font-plex text-[14px] leading-[1.5] text-ink placeholder:text-ink-3"
          />
        </ConfirmDialog>
      )}
    </div>
  );
}
