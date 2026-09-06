import type { Description } from "../lib/attention";
import { formatAge } from "../lib/format";
import { ActionButton } from "./ActionButton";
import { KindPill } from "./KindPill";

/** One item: pill, ticket over project, body over meta, age, actions. */
export function AttentionRow({ kind, project, description }: { kind: string; project: string; description: Description }) {
  const { pill, ticket, body, meta, ageMs, actions } = description;
  return (
    <li
      data-kind={kind}
      className="grid grid-cols-[96px_84px_1fr_60px_auto] items-start gap-[14px] border-t border-needs-you-rule py-3"
    >
      <div>
        <KindPill kind={kind}>{pill}</KindPill>
      </div>
      <div className="min-w-0">
        <div className="truncate font-mono text-[13px] font-semibold text-ink">{ticket ?? "—"}</div>
        <div className="truncate text-[11px] text-faint">{project}</div>
      </div>
      <div className="min-w-0">
        <p className="text-[13px] leading-[1.4] text-body">{body}</p>
        {meta && <p className="text-[12px] text-faint">{meta}</p>}
      </div>
      <div className="text-right font-mono text-[12px] text-muted">{ageMs == null ? "" : formatAge(ageMs)}</div>
      <div className="flex gap-1.5">
        {actions.map((action) => (
          <ActionButton key={action}>{action}</ActionButton>
        ))}
      </div>
    </li>
  );
}
