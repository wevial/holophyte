import { Fragment, useState, type ReactNode } from "react";
import type { MergeQueue, MergeQueueEntry } from "../../lib/contract.gen";
import { useAction } from "../actions";
import { jumpCost } from "../mergeQueue";
import { ActionButton } from "./ActionButton";
import { Btn } from "./Btn";
import { ConfirmDialog } from "./ConfirmDialog";
import { detailLine } from "./CardActions";
import { FactIcon } from "./icons";
import { TitleText } from "./TitleText";
import { useToast } from "./Toast";

const JUMP = "Jump to front";
const REMOVE = "Remove from queue";

/** The project daemon a queue's buttons post to, and whether its `/status` said `actions`. */
export interface QueueDaemon {
  project: string;
  base: string;
  actions: boolean;
}

function JumpIcon() {
  return (
    <svg viewBox="0 0 12 12" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="mr-1.5 inline-block align-[-1px]">
      <path d="M2 1.5h8M6 10.5V4M3.2 6.8 6 4l2.8 2.8" />
    </svg>
  );
}

const PrNumber = ({ number }: { number: number }) => <b className="font-semibold text-ink">#{number}</b>;

/** `#A, #B and #C`, each number bold. */
function Numbers({ numbers }: { numbers: number[] }) {
  return (
    <>
      {numbers.map((number, index) => (
        <Fragment key={number}>
          {index > 0 && (index === numbers.length - 1 ? " and " : ", ")}
          <PrNumber number={number} />
        </Fragment>
      ))}
    </>
  );
}

function Warning({ children }: { children: ReactNode }) {
  return (
    <li data-jump-warning className="flex items-start gap-2.5 border-b border-dashed border-rule-2 py-[9px]">
      <FactIcon ok={false} />
      <span className="min-w-0 break-words">{children}</span>
    </li>
  );
}

function JumpCostLines({ queue, entry }: { queue: MergeQueue; entry: MergeQueueEntry }) {
  const cost = jumpCost(queue, entry);
  const later = cost.estimate == null ? null : `about ${cost.estimate}m`;
  return (
    <>
      <ul aria-label="What jumping costs" className="mt-[18px] border-t border-rule-2 font-mono text-[12px] leading-normal text-ink-2">
        {cost.restarts.length > 0 && (
          <Warning>
            restarts the builds of <Numbers numbers={cost.restarts} />
            {later != null && `; each merges ${later} later`}
          </Warning>
        )}
        {cost.drops != null && <Warning><PrNumber number={cost.drops.number} /> drops {cost.drops.minutes}m of checks already run</Warning>}
        <Warning>needs bypass permission on the repository's merge rules</Warning>
      </ul>
      <p data-jump-next className="mt-3.5 text-[15px] text-ink">
        #{entry.pr.number} builds next{later != null && ` and merges in ${later}`}.
      </p>
    </>
  );
}

/** A row's Jump to front: a confirm saying what jumping costs, then a post;
 *  a refusal keeps the dialog open with the daemon's detail under its buttons. */
export function JumpToFront({ queue, entry, daemon }: { queue: MergeQueue; entry: MergeQueueEntry; daemon: QueueDaemon }) {
  const action = useAction();
  const [open, setOpen] = useState(false);
  const number = entry.pr.number;
  const refused = action.data != null && !action.data.ok ? action.data.detail : null;
  const confirm = async () => {
    const result = await action.mutateAsync({ label: JUMP, base: daemon.base, route: "/actions/queue-jump", body: { pr: number }, said: `#${number} jumped to the front` });
    if (result.ok) setOpen(false);
  };
  return (
    <>
      <ActionButton onAct={async () => { action.reset(); setOpen(true); }}>
        <JumpIcon />
        {JUMP}
      </ActionButton>
      {open && (
        <ConfirmDialog
          label={`Jump · ${daemon.project}`}
          heading={`Jump #${number} to the front?`}
          confirm={JUMP}
          busyLabel="Jumping…"
          busy={action.isPending}
          onConfirm={() => void confirm()}
          onCancel={() => setOpen(false)}
          refusal={refused != null && <p role="alert" data-jump-refusal className={`mt-3 text-bad ${detailLine}`}>{refused}</p>}
        >
          {entry.pr.title != null && <p className="mt-2.5 text-ink-2"><TitleText>{entry.pr.title}</TitleText></p>}
          <JumpCostLines queue={queue} entry={entry} />
        </ConfirmDialog>
      )}
    </>
  );
}

/** A hand pull request's Remove from queue: one post, its answer in the toast. */
export function RemoveFromQueue({ entry, daemon }: { entry: MergeQueueEntry; daemon: QueueDaemon }) {
  const action = useAction();
  const toast = useToast();
  const number = entry.pr.number;
  const remove = async () => {
    const result = await action.mutateAsync({ label: REMOVE, base: daemon.base, route: "/actions/queue-remove", body: { pr: number }, said: `#${number} removed from the queue` });
    if (!result.ok) toast(result.detail);
  };
  return (
    <Btn variant="danger" busy={action.isPending} busyLabel="Removing…" onClick={() => void remove()}>
      {REMOVE}
    </Btn>
  );
}
