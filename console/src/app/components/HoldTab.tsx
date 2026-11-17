import { useState } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import type { Status } from "../../lib/contract.gen";
import { formatSettled, formatStamp } from "../../lib/format";
import { useAction } from "../actions";
import { useHolds } from "../queries";
import { Btn } from "./Btn";
import { LINE, MARK, type LineTone } from "./DayLog";
import { FactIcon } from "./icons";
import { Segmented } from "./Segmented";

const WORDS = { hold: "held", release_hold: "released", disable: "stopped" };
const COLOURS = { hold: "text-wait", release_hold: "text-live", disable: "text-ink-3" };
const TONES: Record<keyof typeof WORDS, LineTone> = { hold: "wait", release_hold: "live", disable: "ink-3" };
const HEADING = "flex h-[30px] items-center gap-2.5 border-b border-hair font-sans text-[13px] font-semibold text-ink";
const EMPTY = "mt-2.5 flex items-center gap-2 font-sans text-[13px] text-ink-3";
const ACTORS = { maintainer: "you", operator: "the operator seat", supervisor: "the supervisor" };

export function HoldTab({ base, project, status }: { base: string; project: string; status: Status }) {
  const history = useHolds(base);
  const rows = [...(history.data?.rows ?? [])].sort((a, b) => b.at - a.at || b.id - a.id);
  const admission = status.admission;
  const held = admission === "held";
  const enabled = admission === "enabled";
  const latest = rows.find((row) => row.action === (held ? "hold" : "disable"));
  const since = latest ? ` since ${formatStamp(latest.at)}` : "";
  const line = enabled
    ? `Claims Ready tickets as its ${status.workers} ${status.workers === 1 ? "worker frees" : "workers free"} up.`
    : held ? `Held${since}. Runs in flight finish; nothing new is claimed.`
      : `Stopped${since}: its loop ends after its live runs.`;
  const colour = enabled ? "text-live" : held ? "text-wait" : "text-ink-3";
  return <div className="mt-6">
    <div className="flex flex-wrap items-start justify-between gap-5">
      <div className="min-w-0 flex-1 basis-[280px]">
        <p className={`flex items-center gap-3 font-sans text-[22px] font-semibold ${colour}`}>
          <span aria-hidden="true" className="size-2.5 shrink-0 rounded-full bg-current" />
          {enabled ? "Taking work" : held ? "Held" : "Stopped"}
        </p>
        <p className="mt-2 font-sans text-[14px] text-ink-2">{line}</p>
        {!enabled && status.hold_note && <p className="mt-2 break-words font-read text-[15px] text-ink-2">“{status.hold_note}”</p>}
      </div>
      {(enabled || held) && <HoldControl key={`${base}:${admission}`} base={base} project={project} held={held} allowed={status.actions === true} />}
    </div>
    <h3 className={`mt-8 ${HEADING}`}>History{history.isSuccess && <span data-count className="font-mono text-[12px] font-normal text-ink-3">{rows.length}</span>}</h3>
    {history.isError ? <p role="alert" className={`${EMPTY} text-ink-2`}>Could not read hold history: {history.error.message}</p>
      : history.isPending ? <p className={EMPTY}>Reading history…</p>
        : rows.length === 0 ? <p className={EMPTY}><FactIcon ok />Never held. It has taken work since it was added.</p>
          : <ol className="ml-[5px] mt-3 font-sans text-[13px] leading-[1.5]">{rows.map((row, index) => {
            const before = rows[index + 1];
            const lasted = row.action === "release_hold" && before?.action === "hold" ? formatSettled(row.at - before.at) : null;
            return <li key={row.id} className={`${LINE} grid gap-x-4 gap-y-1 py-[7px] pl-4 min-[600px]:grid-cols-[120px_minmax(0,1fr)]`}>
              <span data-tone={TONES[row.action]} aria-hidden="true" className={`absolute top-3 -left-1 size-[7px] ${MARK[TONES[row.action]]}`} />
              <time dateTime={new Date(row.at).toISOString()} className="font-mono text-[12px] text-ink-3">{formatStamp(row.at)}</time>
              <p className="min-w-0 break-words">
                <span className={COLOURS[row.action]}>{WORDS[row.action]}{lasted && <> · held <span className="font-mono text-[12px]">{lasted}</span></>}</span>
                <span className="ml-3 text-ink">{row.note}{row.actor && <span className="text-ink-3"> · {ACTORS[row.actor]}</span>}</span>
              </p>
            </li>;
          })}</ol>}
  </div>;
}

function HoldControl({ base, project, held, allowed }: { base: string; project: string; held: boolean; allowed: boolean }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const action = useAction();
  const verb = held ? "Release" : "Hold";
  const close = () => { setOpen(false); setNote(""); action.reset(); };
  const send = async () => {
    if (!allowed || !note.trim() || action.isPending) return;
    const result = await action.mutateAsync({
      base, label: verb, route: held ? "/actions/release-hold" : "/actions/hold", body: { note },
      said: held ? `Released ${project}. Claims resume.` : `Held ${project}. Nothing new is claimed.`,
    });
    if (result.ok) close();
  };
  return <>
    {held ? <Btn disabled={!allowed} title={!allowed ? ACTIONS_OFF : undefined} expanded={open} onClick={() => setOpen(true)}>Release</Btn>
      : <Segmented label="Admission" value="enabled" onPick={(picked) => { if (picked === "held") setOpen(true); }} options={[
        { value: "enabled", label: "Taking work" },
        { value: "held", label: "Held", disabled: !allowed, title: !allowed ? ACTIONS_OFF : undefined, expanded: open },
      ]} />}
    {open && <div className="flex w-full flex-col gap-2 rounded-[10px] bg-raised px-3.5 py-3 shadow-[inset_0_0_0_1px_var(--hair)]">
      <div className="flex flex-col gap-1.5">
      <label htmlFor="hold-reason" className="font-sans text-[13px] font-semibold text-ink">{verb} {project} · reason required, shown on the Overview and saved with the change</label>
      <textarea id="hold-reason" value={note} onChange={(event) => setNote(event.target.value)} disabled={action.isPending}
        placeholder={held ? "Why it should take work again" : "Why it should stop taking work"}
        className="field block min-h-24 w-full font-sans" />
      </div>
      <p className="font-sans text-[13px] text-ink-3">{held ? "Releasing: claims resume once posted." : "Holding: no new claims from now. Runs in flight finish."}</p>
      <div className="flex flex-wrap justify-end gap-2">
        <Btn disabled={action.isPending} onClick={close}>Cancel</Btn>
        <Btn variant="primary" disabled={!allowed || !note.trim() || action.isPending} busy={action.isPending} busyLabel={held ? "Releasing…" : "Holding…"} onClick={() => void send()}>{verb} {project}</Btn>
      </div>
      {action.data && !action.data.ok && <p role="alert" className="break-words font-sans text-[13px] text-bad">{action.data.detail}</p>}
    </div>}
  </>;
}
