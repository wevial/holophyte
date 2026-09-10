import { useEffect, useId, useRef } from "react";
import type { BoardCard } from "../lib/board";
import { STATE_LABELS } from "../lib/board";
import type { HostRecord } from "../lib/hosts";
import { renderMarkdown } from "../lib/markdown";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { useTicket } from "../hooks/useTicket";
import { BoxBar } from "./TimeBoxBar";

/** The Hosts card's line for a daemon that answered 401. */
export const NEEDS_TOKEN = "needs token";
export const NOT_MIRRORED = "not mirrored on this host";

/**
 * A ticket opened from the Board: a fixed panel on the right edge, 480px
 * or the viewport when narrower, over a dimmed backdrop, the page behind
 * untouched. The header carries the identifier, the state and the time
 * box; the body is the daemon's `/tickets/KO-n` rendered from Markdown.
 * Escape, the backdrop or the close button calls `onClose`; focus moves
 * to the panel on open and the caller returns it to the opener.
 */
export function TicketSheet({
  host,
  card,
  onClose,
  deps = defaultPollDeps,
}: {
  host: Pick<HostRecord, "base">;
  card: BoardCard;
  onClose: () => void;
  deps?: { fetch: Fetch };
}) {
  const { state, ticket, error } = useTicket(host.base, card.ticket, deps);
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
          {card.run && <BoxBar elapsedMs={card.run.elapsed_ms} boxMs={card.run.time_box_ms} height={5} />}
        </header>
        <div data-sheet-body className="ticket-body min-h-0 flex-1 overflow-y-auto px-5 py-4 text-[13px] leading-[1.5] text-body">
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
          ) : ticket && ticket.body.trim() === "" ? (
            <p className="text-muted">This ticket was mirrored before the store kept bodies.</p>
          ) : (
            ticket && renderMarkdown(ticket.body)
          )}
        </div>
      </div>
    </div>
  );
}
