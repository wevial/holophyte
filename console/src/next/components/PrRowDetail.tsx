import { useContext, useId, useState } from "react";
import { Link } from "@tanstack/react-router";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import { useAction } from "../actions";
import { useTicket } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { fieldOf, parseBody } from "../ticketForm";
import { Btn } from "./Btn";
import { LimitedMarkdown } from "./LimitedMarkdown";

/** The note box under an opened row: a note posted to `/actions/send-back`
 *  for `run`. A taken note calls `onClose`; a refusal keeps the note and
 *  shows the daemon's detail. */
function SendBackBox({ base, run, label, onClose }: { base: string; run: number; label: string; onClose: () => void }) {
  const action = useAction();
  const [note, setNote] = useState("");
  const noteId = useId();
  const refused = action.data != null && !action.data.ok ? action.data.detail : null;
  const send = async () => {
    const result = await action.mutateAsync({ label: "Send back", base, route: "/actions/send-back", body: { run, note }, said: `Sent ${label} back with your note.` });
    if (result.ok) onClose();
  };
  return (
    <div data-send-back className="basis-full">
      <label htmlFor={noteId} className="block font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">
        Note for the factory
      </label>
      <textarea
        id={noteId}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        rows={3}
        placeholder="What should change before you review it again?"
        className="mt-2 block w-full max-w-[760px] resize-y border border-rule bg-paper px-3 py-2.5 font-read text-[14px] leading-[1.5] text-ink placeholder:text-ink-3"
      />
      <div className="mt-3 flex flex-wrap gap-2">
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn variant="primary" disabled={note.trim() === ""} busy={action.isPending} busyLabel="Sending…" onClick={() => void send()}>
          Send
        </Btn>
      </div>
      {refused != null && (
        <p role="alert" data-action-detail className="m-0 mt-3 max-w-[68ch] break-words font-mono text-[12.5px] leading-normal text-bad">
          {refused}
        </p>
      )}
    </div>
  );
}

/** An opened pull request row: its ticket's What line, then the actions
 *  that move it; Send back only where the daemon takes actions. */
export function PrRowDetail({ base, project, run, item, actions }: { base: string; project: string; run: number; item: AttentionItem; actions: boolean }) {
  const deps = useContext(PollDepsContext);
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const { data } = useTicket(base, ticket, deps);
  const what = data ? fieldOf(parseBody(data.body), "what") : "";
  const [sending, setSending] = useState(false);
  const prUrl = typeof item.pr_url === "string" ? item.pr_url : null;
  return (
    <div data-pr-detail className="flex flex-col gap-3 font-read text-[14px] leading-[1.5] text-ink-2">
      {what !== "" && <LimitedMarkdown lines={2}>{what}</LimitedMarkdown>}
      <div className="flex flex-wrap items-center gap-2">
        {prUrl != null && (
          <a href={prUrl} target="_blank" rel="noreferrer" className="btn" data-variant="primary">
            Open PR to review
          </a>
        )}
        {actions && (
          <Btn expanded={sending} onClick={() => setSending(!sending)}>
            Send back with a note
          </Btn>
        )}
        <Link {...runPage(project, run)} className="btn" data-variant="quiet">
          Open run
        </Link>
        {actions && sending && <SendBackBox base={base} run={run} label={prUrl != null ? prLabel(prUrl) : `run ${run}`} onClose={() => setSending(false)} />}
      </div>
    </div>
  );
}
