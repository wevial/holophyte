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

const ROW = "grid grid-cols-[2.375rem_minmax(0,1fr)_auto] items-center gap-x-3 border-b border-rule-2 py-4 max-[720px]:grid-cols-[2.375rem_minmax(0,1fr)]";
const CONTROL = "justify-self-end max-[720px]:col-start-2 max-[720px]:mt-2 max-[720px]:justify-self-start";

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
  const aside = useAction();
  const refused = [action.data, aside.data].find((data) => data != null && !data.ok) ?? null;
  const setAside = (value: boolean) =>
    void aside.mutateAsync({
      label: value ? "Not now" : "Undo", base, route: "/actions/scout-set-aside", body: { scout, item: item.position, aside: value },
      said: value ? `${id} set aside.` : `${id} is back.`,
    });
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
  if (item.set_aside_ms != null && item.ticket == null) {
    return (
      <li data-scout-action data-set-aside className="border-b border-rule-2 py-2.5">
        <p className="m-0 font-mono text-[12px] text-ink-3">
          <span data-action-id className="font-semibold">{id}</span>
          {" not now · "}
          <button type="button" disabled={!actions || aside.isPending} title={actions ? undefined : ACTIONS_OFF} onClick={() => setAside(false)}
            className="cursor-pointer border-0 bg-transparent p-0 font-mono text-[12px] text-ink-2 underline underline-offset-2 hover:text-ink disabled:cursor-default disabled:no-underline">
            Undo
          </button>
        </p>
        {refused != null && <p role="alert" className={`m-0 mt-1.5 text-bad ${detailLine}`}>{refused.detail}</p>}
      </li>
    );
  }
  return (
    <li data-scout-action className={ROW}>
      <span data-action-id className="font-mono text-[11px] font-semibold leading-none text-ink-3">{id}</span>
      <div className="min-w-0">
        <p className="m-0 flex flex-wrap gap-1.5">
          <span data-action-kind className={TAG}>{item.kind}</span>
          {item.pass > 1 && <span data-action-pass className={TAG}>pass {item.pass}</span>}
        </p>
        <p className="m-0 mt-1.5 font-sans text-[15px] font-semibold leading-[1.35] text-ink [overflow-wrap:anywhere]">
          {item.kind === "revise" ? (
            revise == null ? <span data-action-title>{item.title}</span> : (
              <Link {...revise} data-action-title className="text-ink hover:underline">{item.title}</Link>
            )
          ) : (
            <Link to="/tickets/$project/new" params={{ project }} search={{}} state={{ draft: item.body }} data-action-title className="text-ink hover:underline">
              {item.title}
            </Link>
          )}
        </p>
        <p data-action-why className="m-0 mt-1.5 font-sans text-[14px] leading-[1.45] text-ink-2 [overflow-wrap:anywhere]">{item.why}</p>
        {refused != null && (
          <p role="alert" className={`m-0 mt-1.5 ${refused.ticket == null ? "text-bad" : "text-ink-2"} ${detailLine}`}>{refused.detail}</p>
        )}
      </div>
      <div className={CONTROL}>
        {item.kind === "revise" ? (
          <Btn variant="primary" disabled={!actions || revise == null} title={actions ? undefined : ACTIONS_OFF} onClick={() => revise && void navigate(revise)}>
            Revise
          </Btn>
        ) : item.ticket == null ? (
          <span className="flex gap-2">
            <Btn variant="need" busy={action.isPending} busyLabel="Filing…" disabled={!actions || action.isPending || aside.isPending} title={actions ? undefined : ACTIONS_OFF} onClick={() => void file()}>
              File
            </Btn>
            <Btn disabled={!actions || action.isPending || aside.isPending} title={actions ? undefined : ACTIONS_OFF} onClick={() => setAside(true)}>
              Not now
            </Btn>
          </span>
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

/** How many of `items` are left to file: neither filed nor set aside. */
export const toFile = (items: ScoutAction[]) =>
  items.filter((item) => item.kind !== "revise" && item.ticket == null && item.set_aside_ms == null).length;

/** A scout's action items under `title`, kept for readers only when `titleHidden`: each with File and Not now, its
 *  Filed stamp once filed, one `A2 not now · Undo` line once set aside, and a revise item with Revise, which opens its
 *  ticket's edit page with the item's draft. */
export function ScoutActions({ items, title = "Action items", titleHidden = false, ...scope }: { items: ScoutAction[]; title?: string; titleHidden?: boolean } & Scope) {
  if (items.length === 0) return null;
  const rows = [...items].sort((a, b) => a.position - b.position);
  const open = toFile(rows);
  return (
    <section aria-label={title} className="mt-10">
      <h2 className={`m-0 ${SECTION_LABEL} ${titleHidden ? "sr-only" : ""}`}>
        {title}
        {open > 0 && <span data-to-file className="text-need">{open} to file</span>}
      </h2>
      <ol aria-label="Action items" className={`m-0 list-none p-0 ${titleHidden ? "" : "mt-2"}`}>
        {rows.map((item) => <ActionRow key={item.position} item={item} scope={scope} />)}
      </ol>
    </section>
  );
}
