import { useState, type KeyboardEvent } from "react";
import { ACTIONS_OFF, NOT_WIRED, ROUTES, postAction } from "../lib/actions";
import { OPEN_PR, type Description } from "../lib/attention";
import { formatAge } from "../lib/format";
import type { Fetch } from "../lib/poll";
import type { ThreadRow } from "../lib/threads";
import type { AttentionItem } from "../lib/types";
import { ActionButton } from "./ActionButton";
import { AttemptsCard } from "./AttemptsCard";
import { KindPill } from "./KindPill";
import { QuestionThread } from "./QuestionThread";
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

/** The title of an "Open PR" on a row whose item carried no URL. */
export const NO_PR_URL = "the item carries no PR URL";

/** The page's own `fetch`; `postAction` adds the bearer itself. */
const pageFetch: Fetch = (url, init) => globalThis.fetch(url, init);

/** The daemon a row's buttons post to: its base URL and whether its
 *  `/status` advertised `actions`. `fetch` defaults to the page's own;
 *  tests hand in a fake. */
export interface RowDaemon {
  base: string;
  actions: boolean;
  fetch?: Fetch;
}

/** The body one wired label posts: the row's ticket for "Requeue"
 *  (holophyte/serve.py `requeue_action()`), nothing for the unit actions. */
function bodyFor(label: string, ticket: string | null): Record<string, unknown> {
  return label === "Requeue" && ticket != null ? { ticket } : {};
}

/** One item: pill, ticket over project, body over meta, age, actions. A
 *  row given `thread` toggles its thread card on click; one given
 *  `attempts` wears a "×N" badge by its ticket and toggles its attempts
 *  card the same way; one given `prUrl`
 *  ends its body line with a "PR #N" link that follows without toggling.
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
  daemon,
}: {
  kind: string;
  project: string;
  description: Description;
  thread?: ThreadProps;
  attempts?: AttemptsProps;
  prUrl?: string | null;
  daemon?: RowDaemon;
}) {
  const { pill, ticket, body, meta, ageMs, actions } = description;
  const card = thread ?? attempts;
  const toggle = card?.onToggle;
  const [detail, setDetail] = useState<{ text: string; ok: boolean } | null>(null);
  const act = (label: string) => {
    if (label === OPEN_PR) {
      if (!prUrl) return undefined;
      return async () => {
        window.open(prUrl, "_blank", "noopener,noreferrer");
      };
    }
    const route = ROUTES[label];
    if (!daemon || !daemon.actions || route == null) return undefined;
    return async () => {
      setDetail(null);
      const result = await postAction(daemon.base, route, bodyFor(label, ticket), daemon.fetch ?? pageFetch);
      setDetail({ text: result.detail, ok: result.ok });
    };
  };
  const titleFor = (label: string) => {
    if (label === OPEN_PR) return prUrl ? undefined : NO_PR_URL;
    if (ROUTES[label] == null) return NOT_WIRED;
    if (daemon && !daemon.actions) return ACTIONS_OFF;
    return undefined;
  };
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
            {ticket ?? "—"}
            {attempts && (
              <span data-attempts className="ml-1.5 rounded-[6px] bg-bad-bg px-1.5 text-[11px] font-semibold text-bad-text">
                ×{attempts.runs.length}
              </span>
            )}
          </div>
          <div className="truncate text-[11px] text-faint">{project}</div>
        </div>
        <div className="min-w-0">
          <p className="text-[13px] leading-[1.4] text-body">
            {body}
            {prUrl && (
              <span onClick={(event) => event.stopPropagation()} className="ml-2">
                <PrLink url={prUrl} />
              </span>
            )}
          </p>
          {meta && <p className="text-[12px] text-faint">{meta}</p>}
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
        <div className="flex flex-col items-end gap-1.5">
          <div className="flex gap-1.5">
            {actions.map((action) => (
              <ActionButton key={action} onAct={act(action)} title={titleFor(action)}>
                {action}
              </ActionButton>
            ))}
          </div>
          {detail && (
            <p
              data-action-detail
              data-ok={detail.ok}
              role="status"
              className={`max-w-[280px] text-right text-[12px] ${detail.ok ? "text-muted" : "text-needs-you-link"}`}
            >
              {detail.text}
            </p>
          )}
        </div>
      </div>
      {thread?.open && <QuestionThread rows={thread.rows} />}
      {attempts?.open && <AttemptsCard runs={attempts.runs} />}
    </li>
  );
}
