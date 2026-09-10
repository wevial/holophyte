import type { KeyboardEvent } from "react";
import type { Description } from "../lib/attention";
import { formatAge } from "../lib/format";
import type { ThreadRow } from "../lib/threads";
import { ActionButton } from "./ActionButton";
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

/** One item: pill, ticket over project, body over meta, age, actions. A
 *  row given `thread` toggles its thread card on click; one given `prUrl`
 *  ends its body line with a "PR #N" link that follows without toggling. */
export function AttentionRow({
  kind,
  project,
  description,
  thread,
  prUrl,
}: {
  kind: string;
  project: string;
  description: Description;
  thread?: ThreadProps;
  prUrl?: string | null;
}) {
  const { pill, ticket, body, meta, ageMs, actions } = description;
  const toggle = thread?.onToggle;
  return (
    <li data-kind={kind} className="border-t border-needs-you-rule">
      <div
        {...(thread
          ? {
              role: "button",
              tabIndex: 0,
              "aria-expanded": thread.open,
              onClick: toggle,
              onKeyDown: (event: KeyboardEvent) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  toggle?.();
                }
              },
            }
          : {})}
        className={`grid grid-cols-[96px_84px_1fr_60px_auto] items-start gap-[14px] py-3 ${thread ? "cursor-pointer" : ""}`}
      >
        <div>
          <KindPill kind={kind}>{pill}</KindPill>
        </div>
        <div className="min-w-0">
          <div className="truncate font-mono text-[13px] font-semibold text-ink">{ticket ?? "—"}</div>
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
        </div>
        <div className="text-right font-mono text-[12px] text-muted">{ageMs == null ? "" : formatAge(ageMs)}</div>
        <div className="flex gap-1.5">
          {actions.map((action) => (
            <ActionButton key={action}>{action}</ActionButton>
          ))}
        </div>
      </div>
      {thread?.open && <QuestionThread rows={thread.rows} />}
    </li>
  );
}
