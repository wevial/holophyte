import { useState } from "react";
import { ACTIONS_OFF, postAction } from "../lib/actions";
import type { RowDaemon } from "./RowActions";
import { ActionButton } from "./ActionButton";

/** A lever that must say why: `label` opens a text box (`boxLabel`, by
 *  default "Reason to <label>"), Send stays disabled while it is blank and
 *  posts `body` plus the `note` to `route`, and the daemon's `detail` shows
 *  under it. A daemon without `actions` draws `label` disabled, titled
 *  `ACTIONS_OFF`. */
export function ReasonAction({ daemon, route, body, label, boxLabel = `Reason to ${label.toLowerCase()}` }: {
  daemon: RowDaemon;
  route: string;
  body: Record<string, unknown>;
  label: string;
  boxLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [detail, setDetail] = useState("");
  if (!daemon.actions) return <ActionButton title={ACTIONS_OFF}>{label}</ActionButton>;
  const send = async () => {
    const result = await postAction(daemon.base, route, { ...body, note }, daemon.fetch ?? ((url, init) => globalThis.fetch(url, init)));
    setDetail(result.detail);
    if (result.ok) { setOpen(false); setNote(""); }
  };
  return <div onClick={(event) => event.stopPropagation()}>
    {!open ? <ActionButton onAct={async () => { setOpen(true); }}>{label}</ActionButton> :
      <div className="flex flex-col gap-2">
        <textarea aria-label={boxLabel} value={note} onChange={(event) => setNote(event.target.value)} className="rounded border p-2 text-sm" />
        <div className="flex gap-2">
          <ActionButton onAct={note.trim() ? send : undefined}>Send</ActionButton>
          <button type="button" onClick={() => { setOpen(false); setNote(""); }}>Cancel</button>
        </div>
      </div>}
    {detail && <p role="status" className="text-sm">{detail}</p>}
  </div>;
}
