import { useQuery } from "@tanstack/react-query";
import { useContext, useRef, useState, type KeyboardEvent } from "react";
import { NO_PR_URL } from "../../components/RowActions";
import { ReasonAction } from "../../components/ReasonAction";
import { actionBody, NOT_WIRED, ROOT_LABELS, ROUTES } from "../../lib/actions";
import { ABORT, ABORT_CLOSE, OPEN_PR, RESUME, RUN_SWEEP, type NeedsYouEntry, type PrFacts } from "../../lib/attention";
import { projectName } from "../../lib/derive";
import { UNREACHABLE, type HostRecord } from "../../lib/hosts";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import { classicHref } from "../../lib/view";
import { useAction } from "../actions";
import type { ContractBodies } from "../fleet";
import { readinessQuery } from "../queries";
import { PollDepsContext } from "../scope";
import { Btn } from "./Btn";
import { ConfirmDialog } from "./ConfirmDialog";
import { FactIcon } from "./icons";

type Host = HostRecord<ContractBodies>;

const MERGE = "Merge";
const REASONED = new Set([RESUME, ABORT, ABORT_CLOSE]);

const BUSY: Record<string, string> = {
  Requeue: "Requeuing…",
  [RESUME]: "Resuming…",
  [ABORT]: "Aborting…",
  [ABORT_CLOSE]: "Aborting…",
  "Restart supervisor": "Restarting…",
  [RUN_SWEEP]: "Sweeping…",
  [MERGE]: "Merging…",
};

const ACTIONS_OFF_LINE = "Actions are off on this host. [serve] actions = true turns them on.";

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const str = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

function prOf(item: AttentionItem): string {
  const pr = item.pr != null && typeof item.pr === "object" ? (item.pr as PrFacts) : null;
  const number = num(pr?.number);
  if (number != null) return `PR #${number}`;
  const url = str(item.pr_url);
  return url == null ? "the pull request" : prLabel(url);
}

const projectOf = (host: Host) => projectName(host.project ?? host.label);

/** What the toast says when the daemon takes `label`'s post. */
function saidFor(label: string, item: AttentionItem, host: Host): string {
  const ticket = str(item.ticket);
  const run = num(item.run);
  switch (label) {
    case "Requeue":
      return `Requeued ${ticket}.`;
    case RESUME:
      return `Resumed ${ticket}.`;
    case ABORT:
      return `Aborted run ${run}.`;
    case ABORT_CLOSE:
      return `Aborted run ${run} and closed its pull request.`;
    case "Restart supervisor":
      return `Asked ${projectOf(host)}'s daemon to restart its supervisor.`;
    case RUN_SWEEP:
      return `Started a host sweep on ${str(item.host) ?? host.label}.`;
    case MERGE:
      return `Approved ${prOf(item)}. The loop merges it on its next claim.`;
    default:
      return `${label}: done.`;
  }
}

const detailLine = "max-w-[68ch] break-words font-mono text-[12.5px] leading-normal";

function MergeFacts({ base, run, title, ticket, refused }: { base: string; run: number; title: string | null; ticket: string | null; refused: string | null }) {
  const deps = useContext(PollDepsContext);
  const readiness = useQuery(readinessQuery(base, run, deps.fetch));
  return (
    <>
      {(title != null || ticket != null) && (
        <p className="mt-2.5 text-ink-2">
          {title}
          {title != null && ticket != null && " · "}
          {ticket != null && <span className="font-mono text-[13px]">{ticket}</span>}
        </p>
      )}
      {readiness.data != null && (
        <ul aria-label="Why it is safe" className="mt-[18px] border-t border-rule-2 font-mono text-[12.5px] leading-normal text-ink-2">
          {readiness.data.facts.map((fact) => (
            <li key={fact.name} className="flex items-center gap-2.5 border-b border-dashed border-rule-2 py-[7px]">
              <FactIcon ok={fact.ok} />
              <span className="break-words">{fact.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {refused != null && (
        <p role="alert" className={`mt-3 text-bad ${detailLine}`}>
          {refused}
        </p>
      )}
      <p className="mt-3.5 text-[14px] text-ink-2">The loop merges it on its next claim. The console cannot undo a merge.</p>
    </>
  );
}

const linkBtn = "btn no-underline";

/** The focus card's actions: its first label as the one primary button,
 *  the rest under More, then "Open run" and "Open ticket"; `only="primary"`
 *  draws the primary alone. Each post runs through `useAction()`; a refusal
 *  shows the daemon's detail under them, a taken one calls `onActed`. */
export function CardActions({
  entry,
  host,
  actions,
  title,
  only,
  onActed,
}: {
  entry: NeedsYouEntry;
  host: Host | undefined;
  actions: string[];
  title: string | null;
  only?: "primary";
  onActed?: (said: string) => void;
}) {
  const action = useAction();
  const [reasoning, setReasoning] = useState<string | null>(null);
  const [merging, setMerging] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLButtonElement>(null);
  const mergeRef = useRef<HTMLButtonElement>(null);
  const { item } = entry;
  if (item.kind === UNREACHABLE || host == null) return null;
  const run = num(item.run);
  const ticket = str(item.ticket);
  const ticketUrl = str(item.ticket_url);
  const prUrl = str(item.pr_url);
  const [primary, ...more] = entry.readyToMerge ? [MERGE, ...actions.filter((label) => label !== MERGE)] : actions;
  const on = (primary != null && ROOT_LABELS.has(primary) ? host.host_status?.actions : host.status?.actions) === true;
  const pending = action.isPending ? action.variables?.label : undefined;
  const last = action.variables?.label;
  const refused = action.data != null && !action.data.ok && last != null && !REASONED.has(last) && last !== MERGE ? action.data.detail : null;

  const post = async (label: string, route: string, body: Record<string, unknown>) => {
    const said = saidFor(label, item, host);
    const result = await action.mutateAsync({ label, base: host.base, route, body, said });
    if (result.ok) onActed?.(said);
    return result;
  };

  /** What pressing `label` does, or undefined when it cannot be pressed. */
  const pressOf = (label: string): (() => void) | undefined => {
    if (label === OPEN_PR) return prUrl == null ? undefined : () => window.open(prUrl, "_blank", "noopener,noreferrer");
    if (label === MERGE) return run == null ? undefined : () => { action.reset(); setMerging(true); };
    if (label === RESUME) return ticket == null ? undefined : () => setReasoning(label);
    if (label === ABORT || label === ABORT_CLOSE) return run == null ? undefined : () => setReasoning(label);
    const route = ROUTES[label];
    if (label === "Requeue" && ticket == null) return undefined;
    return route == null ? undefined : () => void post(label, route, actionBody(label, ticket));
  };
  const titleOf = (label: string) => (label === OPEN_PR ? (prUrl == null ? NO_PR_URL : undefined) : ROUTES[label] == null && label !== RESUME ? NOT_WIRED : undefined);

  const closeMore = () => {
    setMoreOpen(false);
    moreRef.current?.focus();
  };
  const escMore = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !moreOpen) return;
    event.preventDefault();
    closeMore();
  };
  const closeMerge = () => {
    setMerging(false);
    mergeRef.current?.focus();
  };
  const confirmMerge = async () => {
    if (run == null) return;
    const result = await post(MERGE, ROUTES[MERGE]!, { run });
    if (result.ok) closeMerge();
  };

  const button = (label: string, variant: "primary" | "danger" | "quiet", onPress?: () => void) => {
    const press = pressOf(label);
    return (
      <Btn
        key={label}
        ref={label === MERGE ? mergeRef : undefined}
        variant={variant}
        busy={pending === label}
        busyLabel={BUSY[label]}
        disabled={press == null}
        title={titleOf(label)}
        onClick={press == null ? undefined : () => { onPress?.(); press(); }}
      >
        {label}
      </Btn>
    );
  };

  return (
    <div data-card-actions className={`${only == null ? "mt-[22px] " : ""}flex flex-col gap-3`}>
      <div className="flex flex-wrap items-center gap-2">
        {on && primary != null && button(primary, primary === ABORT ? "danger" : "primary")}
        {on && only == null && more.length > 0 && (
          <div className="relative" onKeyDown={escMore}>
            <Btn ref={moreRef} expanded={moreOpen} onClick={() => setMoreOpen((open) => !open)}>
              More
            </Btn>
            {moreOpen && (
              <div role="group" aria-label="More actions" className="absolute left-0 top-full z-20 mt-1.5 flex w-max min-w-full flex-col gap-1.5 border border-rule bg-paper p-1.5 shadow-[0_12px_30px_rgba(0,0,0,.18)]">
                {more.map((label) => button(label, "quiet", closeMore))}
              </div>
            )}
          </div>
        )}
        {only == null && run != null && (
          <a className={linkBtn} data-variant="quiet" href={classicHref(`#run=${run}&daemon=${encodeURIComponent(host.base)}`)}>
            Open run
          </a>
        )}
        {only == null && ticketUrl != null && (
          <a className={linkBtn} data-variant="quiet" href={ticketUrl} target="_blank" rel="noreferrer">
            Open ticket
          </a>
        )}
      </div>
      {!on && <p className="font-mono text-[12.5px] text-ink-3">{ACTIONS_OFF_LINE}</p>}
      {on && reasoning != null && (
        <ReasonAction
          key={reasoning}
          daemon={{ base: host.base, actions: true }}
          route={reasoning === RESUME ? "/actions/resume" : ROUTES[reasoning]!}
          body={reasoning === RESUME ? { ticket } : { run, close: reasoning === ABORT_CLOSE }}
          label={reasoning}
          defaultOpen
          post={(route, body) => post(reasoning, route, body)}
          onClose={() => setReasoning(null)}
        />
      )}
      {refused != null && (
        <p role="status" data-action-detail className={`text-bad ${detailLine}`}>
          {refused}
        </p>
      )}
      {merging && run != null && (
        <ConfirmDialog
          label={`Merge · ${projectOf(host)}`}
          heading={`Merge ${prOf(item)}?`}
          confirm={`Merge ${prOf(item)}`}
          busyLabel={BUSY[MERGE]!}
          busy={pending === MERGE}
          onConfirm={() => void confirmMerge()}
          onCancel={closeMerge}
        >
          <MergeFacts
            base={host.base}
            run={run}
            title={title}
            ticket={ticket}
            refused={action.data != null && !action.data.ok && last === MERGE ? action.data.detail : null}
          />
        </ConfirmDialog>
      )}
    </div>
  );
}
