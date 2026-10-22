import { useContext, useEffect, useId, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { prTitle } from "../../lib/attention";
import type { PrOpenItem, PullRequestFacts } from "../../lib/contract.gen";
import type { HostRecord } from "../../lib/hosts";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import { useAction } from "../actions";
import type { ContractBodies } from "../fleet";
import { useTicket } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { fieldOf, parseBody } from "../ticketForm";
import { Btn } from "./Btn";
import { CardActions } from "./CardActions";
import { ConfirmDialog } from "./ConfirmDialog";
import { LimitedMarkdown } from "./LimitedMarkdown";
import { MediaLightbox, type MediaItem } from "./MediaLightbox";

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

const onCi = (item: PrOpenItem): PrOpenItem => ({ ...item, park_kind: "ci", pr: { ...item.pr, state: "ready", checks: "pending" } });

function MarkReady({ base, run, label }: { base: string; run: number; label: string }) {
  const action = useAction();
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const confirm = async () => {
    if (action.isPending) return;
    const result = await action.mutateAsync({ label: "Mark ready for review", base, route: "/actions/mark-ready", body: { run }, said: `Marked ${label} ready for review.`, patch: { run, change: onCi } });
    if (result.ok) setOpen(false);
  };
  return (
    <>
      <Btn ref={opener} onClick={() => { action.reset(); setOpen(true); }}>
        Mark ready for review
      </Btn>
      {open && (
        <ConfirmDialog
          label="Mark ready for review"
          heading={`Mark ${label} ready for review?`}
          confirm="Mark ready for review"
          busyLabel="Marking…"
          busy={action.isPending}
          onCancel={() => { setOpen(false); opener.current?.focus(); }}
          onConfirm={() => void confirm()}
          refusal={action.data && !action.data.ok && <p role="alert" data-action-detail className="mt-3 break-words font-mono text-[12px] text-bad">{action.data.detail}</p>}
        >
          <p className="mt-4 font-read text-[15px] leading-6 text-ink-2">Review bots and the full checks start on GitHub.</p>
        </ConfirmDialog>
      )}
    </>
  );
}

function MediaTile({ item, ref, onOpen }: { item: MediaItem; ref: (node: HTMLButtonElement | null) => void; onOpen: () => void }) {
  const [failed, setFailed] = useState(false);
  return (
    <button
      ref={ref}
      type="button"
      data-media-tile={item.kind}
      data-screenshot={item.kind === "image" ? "" : undefined}
      aria-label={item.kind === "video" ? `Video: ${item.caption}` : item.caption}
      onClick={onOpen}
      className="flex w-[132px] cursor-pointer flex-col gap-1 border-0 bg-transparent p-0 text-left"
    >
      {item.kind === "video" ? (
        <span className="flex h-[80px] w-[132px] items-center justify-center border border-rule bg-paper-2">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-paper shadow-[0_1px_3px_rgba(0,0,0,.3)]">
            <svg aria-hidden="true" viewBox="0 0 10 10" className="ml-px h-2.5 w-2.5 fill-ink">
              <path d="M2 1 9 5 2 9Z" />
            </svg>
          </span>
        </span>
      ) : (
        !failed && <img src={item.url} alt={item.caption} loading="lazy" onError={() => setFailed(true)} className="block h-[80px] w-[132px] border border-rule object-cover" />
      )}
      <span className="break-words font-mono text-[11px] leading-[1.3] text-ink-3">{item.caption}</span>
    </button>
  );
}

export function PrRowDetail({ host, run, item, ready }: { host: HostRecord<ContractBodies>; run: number; item: AttentionItem; ready: boolean }) {
  const { base } = host;
  const project = host.status?.project ?? "";
  const actions = host.status?.actions === true;
  const deps = useContext(PollDepsContext);
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const { data } = useTicket(base, ticket, deps);
  const what = data ? fieldOf(parseBody(data.body), "what") : "";
  const [sending, setSending] = useState(false);
  const prUrl = typeof item.pr_url === "string" ? item.pr_url : null;
  const pr = item.pr != null && typeof item.pr === "object" ? (item.pr as Partial<PullRequestFacts>) : {};
  const media: MediaItem[] = [
    ...(Array.isArray(pr.screenshots) ? pr.screenshots : []).map(({ caption, url }) => ({ kind: "image" as const, caption, url })),
    ...(Array.isArray(pr.videos) ? pr.videos : []).map(({ caption, url }) => ({ kind: "video" as const, caption, url })),
  ];
  const [showing, setShowing] = useState<number | null>(null);
  const tiles = useRef<(HTMLButtonElement | null)[]>([]);
  const runLink = useRef<HTMLAnchorElement>(null);
  const emptied = showing != null && media.length === 0;
  useEffect(() => {
    if (!emptied) return;
    setShowing(null);
    runLink.current?.focus();
  }, [emptied]);
  const preview = typeof pr.preview_url === "string" ? pr.preview_url : null;
  return (
    <div data-pr-detail className="flex flex-col gap-3 font-read text-[14px] leading-[1.5] text-ink-2">
      {what !== "" && <LimitedMarkdown lines={2}>{what}</LimitedMarkdown>}
      {media.length > 0 && (
        <div data-media className="flex flex-wrap gap-2.5">
          {media.map((entry, index) => (
            <MediaTile key={`${index}-${entry.url}`} item={entry} ref={(node) => void (tiles.current[index] = node)} onOpen={() => setShowing(index)} />
          ))}
        </div>
      )}
      {showing != null && media.length > 0 && (
        <MediaLightbox
          items={media}
          start={showing}
          onClose={(index) => {
            setShowing(null);
            (tiles.current[index] ?? runLink.current)?.focus();
          }}
        />
      )}
      <div className="flex flex-wrap items-center gap-2">
        {actions && ready && <CardActions entry={{ item, readyToMerge: true }} host={host} actions={[]} title={prTitle(item)} only="primary" />}
        {prUrl != null && (
          <a href={prUrl} target="_blank" rel="noreferrer" className="btn" data-variant={actions && ready ? "quiet" : "primary"}>
            Open PR to review
          </a>
        )}
        {actions && prUrl != null && pr.state === "draft" && <MarkReady base={base} run={run} label={prLabel(prUrl).replace(/^PR /, "")} />}
        {preview != null && (
          <a href={preview} target="_blank" rel="noreferrer" className="btn" data-variant="quiet">
            Open Vercel preview<span aria-hidden="true"> ↗</span>
          </a>
        )}
        {actions && (
          <Btn expanded={sending} onClick={() => setSending(!sending)}>
            Send back with a note
          </Btn>
        )}
        <Link ref={runLink} {...runPage(project, run)} className="btn" data-variant="quiet">
          Open run
        </Link>
        {actions && sending && <SendBackBox base={base} run={run} label={prUrl != null ? prLabel(prUrl) : `run ${run}`} onClose={() => setSending(false)} />}
      </div>
    </div>
  );
}
