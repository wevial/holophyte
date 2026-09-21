import { useState } from "react";
import { postAction } from "../lib/actions";
import type { RowDaemon } from "./RowActions";
import { ActionButton } from "./ActionButton";

/** Private maintainer feedback; only the daemon receives this text. */
export function SendBackNote({ daemon, runId }: { daemon: RowDaemon; runId: number }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [detail, setDetail] = useState("");
  const send = async () => {
    const result = await postAction(daemon.base, "/actions/send-back", { run: runId, note }, daemon.fetch ?? globalThis.fetch);
    setDetail(result.detail);
    if (result.ok) { setOpen(false); setNote(""); }
  };
  return <div onClick={(event) => event.stopPropagation()}>
    {!open ? <ActionButton onAct={async () => { setOpen(true); }}>Send back with note</ActionButton> :
      <div className="flex flex-col gap-2">
        <textarea aria-label="Maintainer's note" value={note} onChange={(event) => setNote(event.target.value)} className="rounded border p-2 text-sm" />
        <div className="flex gap-2">
          <ActionButton onAct={note.trim() ? send : undefined}>Send</ActionButton>
          <button type="button" onClick={() => { setOpen(false); setNote(""); }}>Cancel</button>
        </div>
      </div>}
    {detail && <p role="status" className="text-sm">{detail}</p>}
  </div>;
}
