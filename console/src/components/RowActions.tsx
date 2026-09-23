import { useState } from "react";
import { ACTIONS_OFF, NOT_WIRED, ROUTES, postAction } from "../lib/actions";
import { ABORT, ABORT_CLOSE, OPEN_PR, RESUME } from "../lib/attention";
import type { Fetch } from "../lib/poll";
import { ActionButton } from "./ActionButton";
import { ReasonAction } from "./ReasonAction";
import { SendBackNote } from "./SendBackNote";

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

/** Shared action wiring for attention rows and the pull request table. */
export function RowActions({ kind, actions, ticket, prUrl, daemon, runId }: {
  kind: string;
  actions: string[];
  ticket: string | null;
  prUrl?: string | null;
  daemon?: RowDaemon;
  runId?: number;
}) {
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
    <div className="flex flex-col items-end gap-1.5">
      <div className="flex gap-1.5">
        {actions.map((action) => action === RESUME && daemon && ticket != null ? (
          <ReasonAction key={action} daemon={daemon} route="/actions/resume" body={{ ticket }} label={action} />
        ) : (action === ABORT || action === ABORT_CLOSE) && daemon && runId != null ? (
          <ReasonAction key={action} daemon={daemon} route={ROUTES[action]!} body={{ run: runId, close: action === ABORT_CLOSE }} label={action} />
        ) : (
          <ActionButton key={action} onAct={act(action)} title={titleFor(action)}>
            {action}
          </ActionButton>
        ))}
      </div>
      {kind === "pr_open" && daemon?.actions && runId != null && <SendBackNote daemon={daemon} runId={runId} />}
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
  );
}
