import { formatDuration } from "../lib/format";
import { agentMs, boxClock } from "../lib/runs";
import { useEffect, useId, useRef, useState } from "react";
import type { BoardCard } from "../lib/board";
import { STATE_LABELS } from "../lib/board";
import type { HostRecord } from "../lib/hosts";
import { Markdown } from "./Markdown";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { useTicket } from "../hooks/useTicket";
import { cancelTicket, editTicket, moveTicket, type WriteResult } from "../lib/boardWrites";
import { BoxBar } from "./TimeBoxBar";

/** The Hosts card's line for a daemon that answered 401. */
export const NEEDS_TOKEN = "needs token";
export const NOT_MIRRORED = "not mirrored on this host";
/** The banner a 409 raises: another tab or the command line wrote first. */
export const CHANGED = "This ticket changed since you opened it";

const button = "rounded-button border border-chip-border px-3 py-1 text-[12px] font-semibold text-ink disabled:opacity-60";

/**
 * A ticket opened from the Board: a fixed panel on the right edge, 480px
 * or the viewport when narrower, over a dimmed backdrop, the page behind
 * untouched. The header carries the identifier, the state and the time
 * box; the body is the daemon's `/tickets/KO-n` rendered from Markdown.
 * Escape, the backdrop or the close button calls `onClose`; focus moves
 * to the panel on open and the caller returns it to the opener.
 *
 * Given `editable` (the host's `/board` said so), the header adds Edit,
 * Move and Cancel, each writing at the `current` revision the sheet read:
 * Edit swaps the body for a textarea with Save and Discard, Move sends the
 * ticket to the column it is not in, and Cancel asks for a note, its
 * confirm naming the card's live run it ends. A write refetches the
 * ticket; a 409 raises the changed banner, whose Reload refetches and
 * drops the edit; a 422 lists its problems.
 */
export function TicketSheet({
  host,
  card,
  onClose,
  editable = false,
  deps = defaultPollDeps,
}: {
  host: Pick<HostRecord, "base">;
  card: BoardCard;
  onClose: () => void;
  editable?: boolean;
  deps?: { fetch: Fetch };
}) {
  const [reloads, setReloads] = useState(0);
  const { state, ticket, error } = useTicket(host.base, card.ticket, deps, reloads);
  // The open write, the body being edited or the cancel's note, each bound
  // to the revision it was opened at: a reload landing a newer revision
  // under an open edit must not let the older text save over it.
  const [draft, setDraft] = useState<{ body: string; revision: number } | null>(null);
  const [note, setNote] = useState<{ text: string; revision: number } | null>(null);
  const [conflict, setConflict] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    panel.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const status = ticket?.status ?? card.status;
  const stateLabel = status in STATE_LABELS ? STATE_LABELS[status as keyof typeof STATE_LABELS] : status;
  const boxMs = ticket?.time_box_ms ?? card.run?.time_box_ms ?? null;
  const title = ticket?.title ?? card.title ?? "";
  const revision = ticket?.current?.revision ?? 0;
  const target = ticket?.current?.column === "backlog" ? "ready" : "backlog";
  const confirm = `Cancel ${card.ticket}${card.runId == null ? "" : ` and end run ${card.runId}`}`;

  /** Fetch the ticket again and drop whatever write was open. */
  const reload = () => {
    setDraft(null);
    setNote(null);
    setConflict(false);
    setProblems([]);
    setFailure(null);
    setReloads((count) => count + 1);
  };

  const write = async (send: () => Promise<WriteResult>) => {
    if (pending) return;
    setPending(true);
    const result = await send();
    setPending(false);
    if (result.ok) return reload();
    if ("conflict" in result) return setConflict(true);
    setProblems("problems" in result ? result.problems : []);
    setFailure("error" in result ? result.error : null);
  };

  return (
    <div data-ticket-sheet className="fixed inset-0 z-40">
      <div data-backdrop aria-hidden="true" onClick={onClose} className="absolute inset-0 bg-ink/40" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="absolute inset-y-0 right-0 flex w-[480px] max-w-full flex-col overflow-hidden border-l border-line bg-card shadow-card outline-none"
      >
        <header className="flex flex-col gap-[6px] border-b border-line px-5 py-4">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[12px] font-semibold text-ink">{card.ticket}</span>
            <span data-sheet-state className="rounded-chip bg-well px-2 py-[2px] font-mono text-[11px] text-muted">
              {stateLabel}
            </span>
            {editable && ticket?.current != null && (
              <span data-sheet-revision className="font-mono text-[11px] text-faint">
                rev {revision}
              </span>
            )}
            {boxMs != null && (
              <span data-sheet-box className="font-mono text-[11px] text-faint">
                box {Math.round(boxMs / 60_000)}m
              </span>
            )}
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="ml-auto rounded-button border border-chip-border px-2 py-[2px] text-[12px] font-semibold text-ink"
            >
              ×
            </button>
          </div>
          <h2 id={titleId} className="text-[15px] font-semibold leading-[1.35] text-ink">
            {title}
          </h2>
          {editable && ticket != null && (
            <div data-sheet-actions className="flex items-center gap-2">
              <button type="button" disabled={pending || draft != null} onClick={() => setDraft({ body: ticket.current?.body ?? ticket.body, revision })} className={button}>
                Edit
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => void write(() => moveTicket(host.base, card.ticket, revision, target, deps.fetch))}
                className={button}
              >
                {target === "ready" ? "Move to Ready" : "Move to Backlog"}
              </button>
              <button type="button" disabled={pending || note != null} onClick={() => setNote({ text: "", revision })} className={button}>
                Cancel
              </button>
            </div>
          )}
          {note != null && (
            <div data-cancel-note className="flex flex-col gap-2">
              <textarea
                aria-label="Cancel note"
                value={note.text}
                onChange={(event) => setNote({ ...note, text: event.target.value })}
                className="min-h-[60px] rounded-button border border-chip-border bg-well p-2 text-[12px] text-body"
              />
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => setNote(null)} className={button}>
                  Keep
                </button>
                <button
                  type="button"
                  disabled={pending || note.text.trim() === ""}
                  onClick={() => void write(() => cancelTicket(host.base, card.ticket, note.revision, note.text, deps.fetch))}
                  className="rounded-button bg-bad px-3 py-1 text-[12px] font-semibold text-card disabled:opacity-60"
                >
                  {confirm}
                </button>
              </div>
            </div>
          )}
          {conflict && (
            <div data-conflict role="alert" className="flex items-center gap-2 text-[12px] text-bad-text">
              <span>{CHANGED}</span>
              <button type="button" onClick={reload} className={`ml-auto ${button}`}>
                Reload
              </button>
            </div>
          )}
          {problems.length > 0 && (
            <ul data-problems role="alert" className="flex list-disc flex-col gap-1 pl-5 font-mono text-[11px] text-bad-text">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          )}
          {failure != null && (
            <p role="alert" className="font-mono text-[11px] text-bad-text">
              write failed: {failure}
            </p>
          )}
          {card.run && <><span>{boxClock(card.run)} {agentMs(card.run) == null ? "n/a" : formatDuration(agentMs(card.run)!)} · wall {formatDuration(card.run.elapsed_ms)}</span><BoxBar elapsedMs={agentMs(card.run)} boxMs={card.run.time_box_ms} height={5} /></>}
        </header>
        <div data-sheet-body className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-[13px] leading-[1.5] text-body">
          {state === "loading" ? (
            <p className="text-muted">Loading the ticket…</p>
          ) : state === "needs_token" ? (
            <p data-needs-token-line className="font-mono text-[12px] text-muted">
              {NEEDS_TOKEN}
            </p>
          ) : state === "not_mirrored" ? (
            <p className="text-muted">{NOT_MIRRORED}</p>
          ) : state === "error" ? (
            <p role="alert" className="font-mono text-[11px] text-bad-text">
              ticket failed: {error}
            </p>
          ) : draft != null ? (
            <div className="flex h-full flex-col gap-2">
              <textarea
                aria-label="Ticket body"
                value={draft.body}
                onChange={(event) => setDraft({ ...draft, body: event.target.value })}
                spellCheck={false}
                className="min-h-[360px] flex-1 rounded-button border border-chip-border bg-well p-3 font-mono text-[12px] leading-[1.5] text-body"
              />
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => setDraft(null)} className={`ml-auto ${button}`}>
                  Discard
                </button>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => void write(() => editTicket(host.base, card.ticket, draft.revision, draft.body, deps.fetch))}
                  className="rounded-button bg-accent px-3 py-1 text-[12px] font-semibold text-card disabled:opacity-60"
                >
                  Save
                </button>
              </div>
            </div>
          ) : ticket && ticket.body.trim() === "" ? (
            <p className="text-muted">This ticket was mirrored before the store kept bodies.</p>
          ) : (
            ticket && <Markdown>{ticket.body}</Markdown>
          )}
        </div>
      </div>
    </div>
  );
}
