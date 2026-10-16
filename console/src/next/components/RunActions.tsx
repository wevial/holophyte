import { useId, useRef, useState } from "react";
import { ROUTES } from "../../lib/actions";
import { ABORT, RESUME } from "../../lib/attention";
import { prLabel } from "../../lib/shipped";
import { useAction } from "../actions";
import type { RunPage } from "../run";
import { Btn } from "./Btn";
import { ACTIONS_OFF_LINE, BUSY, detailLine } from "./CardActions";
import { ConfirmDialog } from "./ConfirmDialog";

const PAUSE = "Pause";
const REQUEUE = "Requeue";
const NOTE = "from the Run page";
const ENDED = new Set(["failed", "abandoned"]);
const PARKED = new Set(["parked", "on_ci", "ready_to_merge"]);

type Page = Extract<RunPage, { kind: "run" }>;

function labelsOf(page: Page): string[] {
  const { run, chain } = page.detail;
  if (page.state === "paused") return [RESUME];
  if (page.liveRun != null) return page.liveRun.stop_requested == null && !PARKED.has(page.state) ? [PAUSE, ABORT] : [];
  const newest = chain?.runs.at(-1)?.id === run.id;
  return ENDED.has(run.outcome ?? "") && newest && !page.liveTicket ? [REQUEUE] : [];
}

/** Pause, Abort, Resume or Requeue for the Run page's run, as its state
 *  allows, each posted through `useAction()`; Abort first asks for a
 *  reason in a confirm. A refusal reads under the buttons. */
export function RunActions({ page, project }: { page: Page; project: string }) {
  const action = useAction();
  const [aborting, setAborting] = useState(false);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const opener = useRef<HTMLButtonElement>(null);
  const labels = labelsOf(page);
  if (labels.length === 0) return null;
  const { run } = page.detail;
  const pending = action.isPending ? action.variables?.label : undefined;
  const refused = action.data != null && !action.data.ok ? action.data.detail : null;

  const post = (label: string, route: string, body: Record<string, unknown>, said: string) =>
    action.mutateAsync({ label, base: page.host.base, route, body, said });
  const press: Record<string, () => void> = {
    [PAUSE]: () => void post(PAUSE, "/actions/pause", { run: run.id, note: NOTE }, `Pause requested for run ${run.id}.`),
    [RESUME]: () => void post(RESUME, "/actions/resume", { ticket: run.ticket, note: NOTE }, `Resumed ${run.ticket}.`),
    [REQUEUE]: () => void post(REQUEUE, ROUTES[REQUEUE]!, { ticket: run.ticket }, `Requeued ${run.ticket}.`),
    [ABORT]: () => {
      action.reset();
      setReason("");
      setAborting(true);
    },
  };
  const keepRunning = () => {
    setAborting(false);
    opener.current?.focus();
  };
  const confirmAbort = async () => {
    const result = await post(ABORT, ROUTES[ABORT]!, { run: run.id, close: false, note: reason.trim() }, `Aborted run ${run.id}.`);
    if (result.ok) setAborting(false);
  };
  const kept = run.pr_url == null ? "" : ` and ${prLabel(run.pr_url)}`;

  return (
    <div data-run-actions className="flex shrink-0 flex-col items-end gap-2">
      <div className="flex flex-wrap justify-end gap-2">
        {labels.map((label) => (
          <Btn
            key={label}
            ref={label === ABORT ? opener : undefined}
            variant={label === PAUSE ? "quiet" : label === ABORT ? "danger" : "primary"}
            busy={pending === label}
            busyLabel={BUSY[label]}
            disabled={!page.actions}
            title={page.actions ? undefined : ACTIONS_OFF_LINE}
            onClick={press[label]}
          >
            {label}
          </Btn>
        ))}
      </div>
      {refused != null && !aborting && (
        <p role="status" data-action-detail className={`m-0 text-bad ${detailLine}`}>
          {refused}
        </p>
      )}
      {aborting && (
        <ConfirmDialog
          label={`Abort · ${project}`}
          heading={`Abort run ${run.id}?`}
          confirm="Abort run"
          busyLabel={BUSY[ABORT]!}
          busy={pending === ABORT}
          cancelLabel="Keep running"
          confirmVariant="danger-fill"
          confirmDisabled={reason.trim() === ""}
          onConfirm={() => void confirmAbort()}
          onCancel={keepRunning}
          refusal={
            refused != null && action.variables?.label === ABORT && (
              <p role="alert" className={`mt-3 text-bad ${detailLine}`}>
                {refused}
              </p>
            )
          }
        >
          <p className="m-0 mt-3 font-sans text-[14px] leading-[1.5] text-ink-2">
            {page.stops} stops now. {run.branch == null ? "Its branch" : `The branch ${run.branch}`}
            {kept} {kept === "" ? "is" : "are"} kept, and {run.ticket} waits for you to requeue it.
          </p>
          <label htmlFor={reasonId} className="mt-4 block font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">
            Reason · saved with the run
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
