import { useEffect, useRef } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../lib/actions";
import type { ScoutAction } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { useAction } from "../actions";
import { useToast } from "./Toast";
import { Btn } from "./Btn";
import { detailLine } from "./CardActions";
import { SECTION_LABEL } from "./Criteria";

const TAG = "border border-rule px-1.5 py-[3px] font-mono text-[11px] leading-none text-ink-3";

interface Scope {
  base: string;
  project: string;
  scout: number;
  actions: boolean;
}

function ActionRow({ item, scope }: { item: ScoutAction; scope: Scope }) {
  const { base, project, scout, actions } = scope;
  const action = useAction();
  const client = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const id = `A${item.position}`;
  const shown = useRef(true);
  useEffect(() => {
    shown.current = true;
    return () => void (shown.current = false);
  }, []);
  const revises = item.kind === "revise" ? item.revises : null;
  const revise = revises == null ? null : {
    to: "/tickets/$project/$key/edit", params: { project, key: revises.ticket }, search: {},
    state: { draft: item.body, scout, revision: revises.revision },
  } as const;
  const refused = action.data != null && !action.data.ok ? action.data : null;
  const file = async () => {
    const result = await action.mutateAsync({
      label: "File", base, route: "/actions/scout-file", body: { scout, item: item.position },
      said: (_detail, answer) => `Filed ${answer.ticket} in ${project} Backlog.`,
    });
    if (result.ok) return;
    if (result.ticket != null) {
      void client.invalidateQueries({ queryKey: ["route", base] });
    } else if (result.problems != null && shown.current) {
      toast(`The checker refused ${id}: ${result.detail}`);
      void navigate({ to: "/tickets/$project/new", params: { project }, search: {}, state: { draft: item.body } });
    }
  };
  return (
    <li data-scout-action className="grid grid-cols-[2.375rem_minmax(0,1fr)_auto] items-center gap-x-2 border-b border-rule-2 py-3.5 max-[720px]:grid-cols-[2.375rem_minmax(0,1fr)]">
      <span data-action-id className="font-mono text-[11px] font-semibold leading-none text-ink-3">{id}</span>
      <div className="min-w-0">
        <p className="m-0 flex flex-wrap items-baseline gap-x-2 gap-y-1 font-sans text-[15px] font-semibold leading-[1.45] text-ink [overflow-wrap:anywhere]">
          <span data-action-kind className={TAG}>{item.kind}</span>
          {item.kind === "revise" ? (
            revise == null ? <span data-action-title>{item.title}</span> : (
              <Link {...revise} data-action-title className="text-ink hover:underline">{item.title}</Link>
            )
          ) : (
            <Link to="/tickets/$project/new" params={{ project }} search={{}} state={{ draft: item.body }} data-action-title className="text-ink hover:underline">
              {item.title}
            </Link>
          )}
          {item.pass > 1 && <span data-action-pass className={TAG}>pass {item.pass}</span>}
        </p>
        <p data-action-why className="m-0 mt-0.5 font-sans text-[14px] leading-[1.45] text-ink-2 [overflow-wrap:anywhere]">{item.why}</p>
        {refused != null && (
          <p role="alert" className={`m-0 mt-1.5 ${refused.ticket == null ? "text-bad" : "text-ink-2"} ${detailLine}`}>{refused.detail}</p>
        )}
      </div>
      <div className="justify-self-end max-[720px]:col-start-2 max-[720px]:mt-2 max-[720px]:justify-self-start">
        {item.kind === "revise" ? (
          <Btn variant="primary" disabled={!actions || revise == null} title={actions ? undefined : ACTIONS_OFF} onClick={() => revise && void navigate(revise)}>
            Revise
          </Btn>
        ) : item.ticket == null ? (
          <Btn variant="primary" busy={action.isPending} busyLabel="Filing…" disabled={!actions || action.isPending} title={actions ? undefined : ACTIONS_OFF} onClick={() => void file()}>
            File
          </Btn>
        ) : (
          <span data-filed className="whitespace-nowrap font-mono text-[12px] text-ok-ink">
            Filed{" "}
            <Link to="/tickets/$project/$key" params={{ project, key: item.ticket }} search={{}} className="underline underline-offset-2">{item.ticket}</Link>
            {" · Backlog"}
            {item.filed_ms != null && ` · ${formatClock(item.filed_ms)}`}
          </span>
        )}
      </div>
    </li>
  );
}

/** A scout's action items: each with File, or its Filed stamp once filed, and a
 *  revise item with Revise, which opens its ticket's edit page with the item's draft. */
export function ScoutActions({ items, ...scope }: { items: ScoutAction[] } & Scope) {
  if (items.length === 0) return null;
  const rows = [...items].sort((a, b) => a.position - b.position);
  const open = rows.filter((item) => item.kind !== "revise" && item.ticket == null).length;
  return (
    <section aria-label="Action items" className="mt-10">
      <h2 className={`m-0 ${SECTION_LABEL}`}>
        Action items
        {open > 0 && <span data-to-file className="text-need">{open} to file</span>}
      </h2>
      <ol aria-label="Action items" className="m-0 mt-2 list-none p-0">
        {rows.map((item) => <ActionRow key={item.position} item={item} scope={scope} />)}
      </ol>
    </section>
  );
}
