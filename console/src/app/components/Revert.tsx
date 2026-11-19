import { useContext, useId, useState, type ReactNode } from "react";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import type { Reverted } from "../../lib/contract.gen";
import { shortSha } from "../../lib/shipped";
import { previewRevert, useRevert, type RevertAnswer } from "../actions";
import { useRun } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { ConfirmDialog } from "./ConfirmDialog";
import type { MenuItem } from "./MoreMenu";
import { useToast } from "./Toast";
import { detailLine } from "./CardActions";

/** A merged ticket of a project whose status reports `reverts`, its merged
 *  `run`, its merge `sha` when known, its `reverted` once reverted, and
 *  `off` saying why its daemon takes no actions. */
export interface RevertTarget {
  base: string;
  project: string;
  ticket: string;
  run: number | null;
  sha?: string | null;
  reverted?: Reverted | null;
  off?: string | null;
}

type Place = { project: string; run: number } | { project: string; ticket: string };

const routeOf = (place: Place) =>
  "run" in place ? runPage(place.project, place.run) : ({ to: "/tickets/$project/$key", params: { project: place.project, key: place.ticket } } as const);

/** Open ticket or Open run for `open` when given, then Copy link to `copy`. */
export function usePlaceItems(copy: Place, open?: Place): MenuItem[] {
  const navigate = useNavigate();
  const router = useRouter();
  const toast = useToast();
  const copyLink = () => {
    const href = new URL(router.history.createHref(router.buildLocation({ ...routeOf(copy), search: {} }).href), window.location.href).href;
    const copied = navigator.clipboard?.writeText(href) ?? Promise.reject(new Error("no clipboard"));
    void copied.then(
      () => toast("Copied link."),
      () => toast(href),
    );
  };
  const items: MenuItem[] = [{ label: "Copy link", onSelect: copyLink }];
  if (open != null) items.unshift({ label: "run" in open ? "Open run" : "Open ticket", onSelect: () => void navigate(routeOf(open)) });
  return items;
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-[12.5px]">{children}</code>;
}

function Refusal({ answer }: { answer: Extract<RevertAnswer, { kind: "refused" | "failed" }> }) {
  return (
    <div data-revert-refusal className="mt-3 rounded-[6px] bg-bad-wash px-3.5 py-3 font-sans text-[14px] leading-[1.5] text-ink shadow-[inset_0_0_0_1px_var(--bad)]">
      {answer.kind === "refused" && answer.paths.length > 0 ? (
        <>
          It changed{" "}
          {answer.paths.map((path, index) => (
            <span key={path}>
              {index > 0 && ", "}
              <Code>{path}</Code>
            </span>
          ))}
          , which this project never reverts. Ship a forward fix instead.
        </>
      ) : (
        answer.reason
      )}
    </div>
  );
}

function RevertDialog({ target, first, onClose, onFiled }: { target: RevertTarget; first: RevertAnswer; onClose: () => void; onFiled: (ticket: string) => void }) {
  const deps = useContext(PollDepsContext);
  const file = useRevert();
  const [answer, setAnswer] = useState(first);
  const [reason, setReason] = useState("");
  const [failed, setFailed] = useState<string | null>(null);
  const reasonId = useId();
  const read = useRun(target.sha == null && answer.kind === "preview" ? target.base : null, target.run, deps);
  const sha = shortSha(target.sha ?? read.data?.run.merge_sha ?? null);
  const label = target.run == null ? target.project : `${target.project} · run ${target.run}`;
  if (answer.kind !== "preview") {
    return (
      <ConfirmDialog label={label} heading={`${target.ticket} can't be reverted`} cancelLabel="Close" onCancel={onClose}>
        {answer.kind !== "filed" && <Refusal answer={answer} />}
      </ConfirmDialog>
    );
  }
  const send = async () => {
    const filed = await file.mutateAsync({ base: target.base, key: target.ticket, reason: reason.trim() });
    setFailed(filed.kind === "failed" ? filed.reason : null);
    if (filed.kind === "filed") onFiled(filed.ticket);
    else if (filed.kind === "refused") setAnswer(filed);
  };
  return (
    <ConfirmDialog
      label={label}
      heading={`Revert ${target.ticket}?`}
      confirm="File revert"
      busyLabel="Filing…"
      busy={file.isPending}
      cancelLabel="Keep it"
      confirmVariant="danger-fill"
      confirmDisabled={reason.trim() === ""}
      onConfirm={() => void send()}
      onCancel={onClose}
      refusal={
        failed != null && (
          <p role="alert" className={`mt-3 text-bad ${detailLine}`}>
            {failed}
          </p>
        )
      }
    >
      <p className="m-0 mt-2 font-sans text-[14px] leading-[1.5] text-ink-2">
        Files an urgent ticket that reverts {sha === "" ? "its merge" : sha}. It goes through verify and review and merges by this project's rule. {target.ticket} returns to Backlog when it does.
      </p>
      <h3 className="m-0 mt-4 font-sans text-[13px] font-semibold leading-none text-ink">Merged since, same files</h3>
      {answer.dependents.length === 0 ? (
        <p className="m-0 mt-2.5 font-sans text-[13px] leading-[1.45] text-ink-3">Nothing merged since touches these files</p>
      ) : (
        <ul aria-label="Merged since, same files" className="m-0 mt-2.5 flex list-none flex-col gap-1.5 p-0">
          {answer.dependents.map((dependent) => (
            <li key={`${dependent.key} ${dependent.sha}`} data-dependent className="flex flex-wrap gap-x-2.5 font-mono text-[12.5px] leading-[1.45] text-ink-2">
              <span className="font-semibold text-ink">{dependent.key}</span>
              <span>{shortSha(dependent.sha)}</span>
              <span className="min-w-0 [overflow-wrap:anywhere]">{dependent.paths.join(", ")}</span>
            </li>
          ))}
        </ul>
      )}
      <label htmlFor={reasonId} className="mt-4 block font-sans text-[13px] font-semibold leading-none text-ink">
        Reason
      </label>
      <textarea
        id={reasonId}
        value={reason}
        required
        onChange={(event) => setReason(event.target.value)}
        rows={3}
        placeholder="Why revert it?"
        className="field mt-2 block w-full resize-y leading-[1.5]"
      />
    </ConfirmDialog>
  );
}

/** Revert… for `target`, null when it is null, and what it opens: the
 *  confirm after a preview, and once filed a status line naming the new
 *  ticket. Disabled once `reverted`, naming the revert ticket. */
export function useRevertFlow(target: RevertTarget | null): { item: MenuItem | null; layer: ReactNode } {
  const deps = useContext(PollDepsContext);
  const [previewing, setPreviewing] = useState(false);
  const [open, setOpen] = useState<{ target: RevertTarget; answer: RevertAnswer; opener: HTMLElement | null } | null>(null);
  const [filed, setFiled] = useState<{ ticket: string; of: RevertTarget } | null>(null);
  const start = async (on: RevertTarget) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setFiled(null);
    setPreviewing(true);
    const answer = await previewRevert(on.base, on.ticket, deps.fetch);
    setPreviewing(false);
    setOpen({ target: on, answer, opener });
  };
  const close = () => {
    open?.opener?.focus();
    setOpen(null);
  };
  const reverted = target?.reverted;
  const item: MenuItem | null =
    target == null
      ? null
      : {
          label: "Revert…",
          danger: true,
          divided: true,
          disabled: reverted != null || target.off != null || previewing,
          note: reverted != null ? (reverted.by == null ? "Reverted" : `Reverted by ${reverted.by}`) : (target.off ?? undefined),
          onSelect: () => void start(target),
        };
  const layer = (
    <>
      {open != null && (
        <RevertDialog
          target={open.target}
          first={open.answer}
          onClose={close}
          onFiled={(ticket) => {
            setFiled({ ticket, of: open.target });
            close();
          }}
        />
      )}
      {filed != null && (
        <p role="status" data-revert-filed className={`m-0 text-ink-2 ${detailLine}`}>
          Filed{" "}
          <Link to="/tickets/$project/$key" params={{ project: filed.of.project, key: filed.ticket }} className="font-medium text-ink underline underline-offset-2">
            {filed.ticket}
          </Link>{" "}
          to revert {filed.of.ticket}.
        </p>
      )}
    </>
  );
  return { item, layer };
}
