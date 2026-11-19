import { useContext, useId, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import type { TicketDetail } from "../../lib/contract.gen";
import type { HostRecord } from "../../lib/hosts";
import { useBoardWrite, writeRefusal, type BoardWrite } from "../actions";
import type { ContractBodies } from "../fleet";
import { useBoards } from "../queries";
import { PollDepsContext } from "../scope";
import { Btn } from "./Btn";
import { ConfirmDialog } from "./ConfirmDialog";
import type { MenuItem } from "./MoreMenu";

const SETTLED = new Set(["merged", "abandoned"]);

function Refusal({ lines, className }: { lines: string[] | null; className: string }) {
  if (lines == null) return null;
  return (
    <div role="alert" data-move-refusal className={className}>
      {lines.map((line) => (
        <p key={line} className="m-0 max-w-[68ch] break-words font-mono text-[12.5px] leading-normal text-bad">
          {line}
        </p>
      ))}
    </div>
  );
}

/** Edit, Move to backlog, Requeue and Cancel ticket for `ticketKey`, drawn only
 *  while `host`'s `/board` takes writes, each posted at the revision the
 *  page read: the ticket's current one, else its board card's. Edit is a
 *  link to the Edit page, or, given `onEditRaw`, a button that opens the
 *  raw editor at that revision, disabled while `editOpen`. Given `menu`, it
 *  draws `menu` with the same moves as menu items in place of the buttons. */
export function TicketMoves({ project, ticketKey, ticket, host, liveRun, onEditRaw, editOpen, menu }: {
  project: string; ticketKey: string; ticket: TicketDetail; host: HostRecord<ContractBodies>; liveRun: number | null;
  onEditRaw?: (revision: number) => void; editOpen: boolean; menu?: (items: MenuItem[]) => ReactNode;
}) {
  const navigate = useNavigate();
  const deps = useContext(PollDepsContext);
  const board = useBoards([host], deps)[0]?.board;
  const write = useBoardWrite();
  const [last, setLast] = useState<"backlog" | "ready" | "cancel" | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const opener = useRef<HTMLButtonElement>(null);
  const menuOpener = useRef<HTMLElement | null>(null);
  const card = board?.columns.flatMap((column) => column.tickets).find((each) => each.ticket === ticketKey);
  const revision = ticket.current?.revision ?? card?.revision ?? null;
  if (board?.editable !== true || revision == null || SETTLED.has(ticket.status)) return menu?.([]) ?? null;
  const column = ticket.current?.column ?? card?.column ?? null;
  const refused = writeRefusal(write.data, ticketKey);

  const at = { base: host.base, key: ticketKey, revision };
  const post = (kind: "backlog" | "ready" | "cancel", body: BoardWrite) => {
    setLast(kind);
    return write.mutateAsync(body);
  };
  const move = (target: "backlog" | "ready") =>
    void post(target, { ...at, column: target, said: `Moved ${ticketKey} to ${target === "ready" ? "Ready" : "Backlog"}.` });
  const confirmCancel = async () => {
    const result = await post("cancel", { ...at, note: reason.trim(), said: `Cancelled ${ticketKey}.` });
    if (result.ok) setCancelling(false);
  };
  const keepIt = () => {
    setCancelling(false);
    (opener.current ?? menuOpener.current)?.focus();
  };
  const openCancel = () => {
    menuOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    write.reset();
    setReason("");
    setCancelling(true);
  };
  const toBacklog = ticket.run == null && column !== "backlog";
  const items: MenuItem[] = [
    onEditRaw != null
      ? { label: "Edit", onSelect: () => onEditRaw(revision), disabled: editOpen }
      : { label: "Edit", onSelect: () => void navigate({ to: "/tickets/$project/$key/edit", params: { project, key: ticketKey }, search: {} }) },
    ...(toBacklog ? [{ label: "Move to backlog", onSelect: () => move("backlog"), disabled: write.isPending }] : []),
    ...(column === "backlog" ? [{ label: "Requeue", onSelect: () => move("ready"), disabled: write.isPending }] : []),
    { label: "Cancel ticket", onSelect: openCancel, disabled: write.isPending },
  ];

  return (
    <>
      {menu != null ? (
        menu(items)
      ) : (
      <>
      {onEditRaw != null ? (
        <Btn disabled={editOpen} onClick={() => onEditRaw(revision)}>Edit</Btn>
      ) : (
        <Link to="/tickets/$project/$key/edit" params={{ project, key: ticketKey }} search={{}} className="btn" data-variant="quiet">Edit</Link>
      )}
      {toBacklog && (
        <Btn busy={write.isPending && last === "backlog"} busyLabel="Moving…" disabled={write.isPending && last !== "backlog"} onClick={() => move("backlog")}>
          Move to backlog
        </Btn>
      )}
      {column === "backlog" && (
        <Btn busy={write.isPending && last === "ready"} busyLabel="Requeuing…" disabled={write.isPending && last !== "ready"} onClick={() => move("ready")}>
          Requeue
        </Btn>
      )}
      <Btn ref={opener} variant="danger" disabled={write.isPending} onClick={openCancel}>
        Cancel ticket
      </Btn>
      </>
      )}
      {!cancelling && last !== "cancel" && <Refusal lines={refused} className="basis-full [contain:inline-size]" />}
      {cancelling && (
        <ConfirmDialog
          label={`Cancel · ${project}`}
          heading={`Cancel ${ticketKey}?`}
          confirm="Cancel ticket"
          busyLabel="Cancelling…"
          busy={write.isPending}
          cancelLabel="Keep it"
          confirmVariant="danger-fill"
          confirmDisabled={reason.trim() === ""}
          onConfirm={() => void confirmCancel()}
          onCancel={keepIt}
          refusal={<Refusal lines={refused} className="mt-3" />}
        >
          <p className="m-0 mt-3 max-w-[75ch] font-read text-[14px] leading-[1.5] text-ink-2">
            {liveRun != null ? `Run ${liveRun} stops now. Its branch and pull request are kept, and the ticket leaves the board.` : "The ticket leaves the board. Nothing runs."}
          </p>
          <label htmlFor={reasonId} className="mt-4 block font-sans text-[13px] font-semibold leading-none text-ink">
            Reason · saved with the ticket
          </label>
          <textarea
            id={reasonId}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={3}
            placeholder="Why cancel it?"
            className="field mt-2 block w-full resize-y font-read leading-[1.5]"
          />
        </ConfirmDialog>
      )}
    </>
  );
}
