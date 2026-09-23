import { PrFacts } from "./PrFacts";
import { TicketLink } from "./TicketLink";
import { useState, type KeyboardEvent } from "react";
import type { Description } from "../lib/attention";
import { formatAge } from "../lib/format";
import type { ThreadRow } from "../lib/threads";
import type { AttentionItem } from "../lib/types";
import { RowActions, type RowDaemon } from "./RowActions";
import { AttemptsCard } from "./AttemptsCard";
import { KindPill } from "./KindPill";
import { QuestionThread } from "./QuestionThread";
import { RunDetail } from "./RunDetail";
import { PrLink } from "./ShippedTable";

/** A question row's thread, when the daemon serves `/ledger`: its rows,
 *  whether it is the one open thread, and the toggle. */
export interface ThreadProps {
  rows: ThreadRow[];
  open: boolean;
  onToggle: () => void;
}

/** A failed ticket's attempts, when it failed more than once: the runs
 *  oldest first, whether it is the one open card, and the toggle. */
export interface AttemptsProps {
  runs: AttentionItem[];
  open: boolean;
  onToggle: () => void;
}

export { NO_PR_URL, type RowDaemon } from "./RowActions";

/** A question clamps to four lines (KO-717); one past four text lines, or
 *  longer than four lines of this column hold, gets "more". */
const CLAMP_LINES = 4;
const CLAMP_CHARS = 320;

function runsLong(text: string): boolean {
  return text.length > CLAMP_CHARS || text.split("\n").length > CLAMP_LINES;
}

/** One item: pill, ticket over project, body over meta, age, actions. A
 *  row given `thread` toggles its thread card on click; one given
 *  `attempts` wears a "×N" badge by its ticket and toggles its attempts
 *  card the same way; one given `prUrl` opens its body line with a
 *  "PR #N" link that follows without toggling, and one whose description
 *  carries `facts` draws them as chips under it.
 *  A failed row given `runId` and `daemon` opens that run’s detail card.
 *  A row given `daemon` posts each wired label (`lib/actions.ts` ROUTES)
 *  to it on click and shows the reply's `detail` under the buttons; the
 *  next poll redraws the row. Labels without a route, and every label of
 *  a daemon without `actions`, are drawn disabled with a title saying why.
 *  "Open PR" (a `pr_open` row) is the exception: it opens `prUrl` in a
 *  new tab, posts nothing, and needs no daemon. */
export function AttentionRow({
  kind,
  project,
  description,
  thread,
  attempts,
  prUrl,
  ticketUrl,
  daemon,
  runId,
  now = Date.now(),
}: {
  kind: string;
  project: string;
  description: Description;
  thread?: ThreadProps;
  attempts?: AttemptsProps;
  prUrl?: string | null;
  ticketUrl?: string | null;
  daemon?: RowDaemon;
  runId?: number;
  now?: number;
}) {
  const { pill, ticket, body, meta, ageMs, actions, facts } = description;
  const [expanded, setExpanded] = useState(false);
  const [more, setMore] = useState(false);
  const clamped = kind === "blocked" && runsLong(body) && !more;
  const failed = kind === "failed" && runId != null && daemon != null && !thread && !attempts;
  const card = thread ?? attempts ?? (failed ? { open: expanded, onToggle: () => setExpanded((value) => !value) } : undefined);
  const toggle = card?.onToggle;
  // A row with fact chips leads with the PR link (KO-370); any other row
  // keeps the link after the prose, as before.
  const leadsWithLink = facts != null && facts.length > 0;
  return (
    <li data-kind={kind} className="border-t border-needs-you-rule">
      <div
        {...(card
          ? {
              role: "button",
              tabIndex: 0,
              "aria-expanded": card.open,
              onClick: toggle,
              onKeyDown: (event: KeyboardEvent) => {
                if (event.target !== event.currentTarget) return;
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  toggle?.();
                }
              },
            }
          : {})}
        className={`grid grid-cols-[96px_84px_1fr_60px_auto] items-start gap-[14px] py-3 ${card ? "cursor-pointer" : ""}`}
      >
        <div>
          <KindPill kind={kind}>{pill}</KindPill>
        </div>
        <div className="min-w-0">
          <div className="truncate font-mono text-[13px] font-semibold text-ink">
            <TicketLink ticket={ticket ?? "—"} ticket_url={ticketUrl} />
            {attempts && (
              <span data-attempts className="ml-1.5 rounded-[6px] bg-bad-bg px-1.5 text-[11px] font-semibold text-bad-text">
                ×{attempts.runs.length}
              </span>
            )}
          </div>
          <div className="truncate text-[11px] text-faint">{project}</div>
        </div>
        <div className="min-w-0">
          <p
            data-body
            className={`text-[13px] leading-[1.4] text-body ${kind === "blocked" ? "whitespace-pre-line break-words" : ""} ${clamped ? "line-clamp-4" : ""}`}
          >
            {prUrl && leadsWithLink && (
              <span onClick={(event) => event.stopPropagation()} className="mr-2">
                <PrLink url={prUrl} />
              </span>
            )}
            {body}
            {prUrl && !leadsWithLink && (
              <span onClick={(event) => event.stopPropagation()} className="ml-2">
                <PrLink url={prUrl} />
              </span>
            )}
          </p>
          {kind === "blocked" && runsLong(body) && (
            <button
              type="button"
              aria-expanded={more}
              onClick={(event) => {
                event.stopPropagation();
                setMore((value) => !value);
              }}
              className="text-[12px] font-semibold text-needs-you-link"
            >
              {more ? "less" : "more"}
            </button>
          )}
          <PrFacts facts={facts} />
          {meta && <p className="text-[12px] text-faint">{meta}</p>}
          {failed && !attempts && (
            <p className="text-[12px] font-semibold text-needs-you-link">{card?.open ? "hide run ▴" : "run ▾"}</p>
          )}
          {thread && (
            <p data-thread-hint className="text-[12px] font-semibold text-needs-you-link">
              {thread.open ? "hide thread ▴" : "thread ▾"}
            </p>
          )}
          {attempts && (
            <p data-attempts-hint className="text-[12px] font-semibold text-needs-you-link">
              {attempts.open ? "hide attempts ▴" : "attempts ▾"}
            </p>
          )}
        </div>
        <div className="text-right font-mono text-[12px] text-muted">{ageMs == null ? "" : formatAge(ageMs)}</div>
        <RowActions kind={kind} actions={actions} ticket={ticket} prUrl={prUrl} daemon={daemon} runId={runId} />
      </div>
      {thread?.open && <QuestionThread rows={thread.rows} />}
      {attempts?.open && <AttemptsCard runs={attempts.runs} />}
      {failed && card?.open && <RunDetail base={daemon.base} id={runId} now={now} polls={0} deps={daemon.fetch ? { fetch: daemon.fetch } : undefined} />}
    </li>
  );
}
