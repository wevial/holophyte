import { useState } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import type { Status } from "../../lib/contract.gen";
import { formatStamp } from "../../lib/format";
import { useAction } from "../actions";
import { useHolds } from "../queries";
import { Btn } from "./Btn";

const WORDS = { hold: "held", release_hold: "released", disable: "stopped" };
const COLOURS = { hold: "text-wait", release_hold: "text-live", disable: "text-ink-3" };
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
          <span aria-hidden="true" className="h-2.5 w-2.5 shrink-0 bg-current" />
          {enabled ? "Taking work" : held ? "Held" : "Stopped"}
        </p>
        <p className="mt-2 font-read text-[15px] text-ink-2">{line}</p>
        {!enabled && status.hold_note && <p className="mt-2 break-words font-read text-[15px] text-ink-2">“{status.hold_note}”</p>}
      </div>
      {(enabled || held) && <HoldControl key={`${base}:${admission}`} base={base} project={project} held={held} allowed={status.actions === true} />}
    </div>
    <h3 className="mt-8 border-b border-rule-2 pb-3 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">History</h3>
    {history.isError ? <p role="alert" className="mt-4 font-read text-[14px] text-ink-2">Could not read hold history: {history.error.message}</p>
      : history.isPending ? <p className="mt-4 font-read text-[14px] text-ink-3">Reading history…</p>
        : rows.length === 0 ? <p className="mt-4 font-read text-[14px] text-ink-3">Never held. It has taken work since it was added.</p>
          : <ol>{rows.map((row) => <li key={row.id} className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-2 border-b border-rule-2 py-4 min-[600px]:grid-cols-[120px_65px_1fr]">
            <time dateTime={new Date(row.at).toISOString()} className="font-mono text-[13px] text-ink-2">{formatStamp(row.at)}</time>
            <span className={`font-mono text-[13px] ${COLOURS[row.action]}`}>{WORDS[row.action]}</span>
            <p className="col-span-2 min-w-0 break-words font-read text-[14px] min-[600px]:col-span-1">{row.note}{row.actor && <span className="text-ink-3"> · {ACTORS[row.actor]}</span>}</p>
          </li>)}</ol>}
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
      : <div role="group" aria-label="Admission" className="flex border border-rule font-mono text-[12px]">
        <button type="button" aria-pressed="true" className="min-h-10 bg-live-wash px-3 text-live">Taking work</button>
        <button type="button" aria-pressed="false" aria-expanded={open} disabled={!allowed} title={!allowed ? ACTIONS_OFF : undefined} onClick={() => setOpen(true)} className="min-h-10 border-l border-rule px-3 text-ink-2 disabled:opacity-50">Held</button>
      </div>}
    {open && <div className="w-full border border-rule bg-paper-2 p-4">
      <label htmlFor="hold-reason" className="font-read text-[14px] text-ink-2">{verb} {project} · reason required, shown on the Overview and saved with the change</label>
      <textarea id="hold-reason" value={note} onChange={(event) => setNote(event.target.value)} disabled={action.isPending}
        placeholder={held ? "Why it should take work again" : "Why it should stop taking work"}
        className="mt-3 min-h-24 w-full border border-rule bg-paper p-3 font-read text-[15px] text-ink" />
      <p className="mt-2 font-read text-[14px] text-ink-2">{held ? "Releasing: claims resume once posted." : "Holding: no new claims from now. Runs in flight finish."}</p>
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Btn disabled={action.isPending} onClick={close}>Cancel</Btn>
        <Btn variant="primary" disabled={!allowed || !note.trim() || action.isPending} busy={action.isPending} busyLabel={held ? "Releasing…" : "Holding…"} onClick={() => void send()}>{verb} {project}</Btn>
      </div>
      {action.data && !action.data.ok && <p role="alert" className="mt-3 break-words font-read text-[14px] text-bad">{action.data.detail}</p>}
    </div>}
  </>;
}
