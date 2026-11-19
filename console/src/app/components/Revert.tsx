import { useContext, useId, useState, type ReactNode } from "react";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { runDetailSchema, type Reverted } from "../../lib/contract.gen";
import type { Fetch } from "../../lib/poll";
import { shortSha } from "../../lib/shipped";
import { previewRevert, useRevert, type RevertAnswer } from "../actions";
import { fetchRoute, ticketQuery } from "../queries";
import { runPage } from "../run";
import { keepScope, PollDepsContext } from "../scope";
import { ConfirmDialog } from "./ConfirmDialog";
import type { MenuItem } from "./MoreMenu";
import { useToast } from "./Toast";
import { detailLine } from "./CardActions";

/** A merged ticket of a project whose status reports `reverts`, its merged
 *  `run`, `newest` when that run was offered as its newest attempt, its
 *  `reverted` once reverted, and `off` saying why its daemon takes none. */
interface RevertTarget {
  base: string;
  project: string;
  ticket: string;
  run: number | null;
  newest?: boolean;
  reverted?: Reverted | null;
  off?: string | null;
}

type Place = { project: string; run: number } | { project: string; ticket: string };

/** Copies `text` through a selected, offscreen textarea: the Clipboard API
 *  is absent on a plain-HTTP origin such as a private-network console. */
function copyBySelection(text: string): boolean {
  const back = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.focus();
  area.select();
  let done = false;
  try {
    done = document.execCommand("copy");
  } catch {
    done = false;
  }
  area.remove();
  back?.focus();
  return done;
}

const routeOf = (place: Place) =>
  "run" in place ? runPage(place.project, place.run) : ({ to: "/tickets/$project/$key", params: { project: place.project, key: place.ticket }, search: keepScope } as const);

/** Open ticket or Open run for `open` when given, then Copy link to `copy`. */
export function usePlaceItems(copy: Place, open?: Place): MenuItem[] {
  const navigate = useNavigate();
  const router = useRouter();
  const toast = useToast();
  const copyLink = () => {
    const href = new URL(router.history.createHref(router.buildLocation({ ...routeOf(copy), search: {} }).href), window.location.href).href;
    const copied = window.isSecureContext && navigator.clipboard != null ? navigator.clipboard.writeText(href).then(() => true, () => copyBySelection(href)) : Promise.resolve(copyBySelection(href));
    void copied.then((done) => toast(done ? "Copied link." : href));
  };
  const items: MenuItem[] = [{ label: "Copy link", onSelect: copyLink }];
  if (open != null) items.unshift({ label: "run" in open ? "Open run" : "Open ticket", onSelect: () => void navigate(routeOf(open)) });
  return items;
}

type Checked = { refusal: Extract<RevertAnswer, { kind: "failed" }> } | { sha: string };

const failure = (reason: string): Checked => ({ refusal: { kind: "failed", reason } });

/** The merge sha a revert of `on` would undo, or why `on` can't be
 *  reverted from here. The daemon reverts the ticket's latest merged run
 *  with a recorded sha and is sent only the key, so `on.run` must be the
 *  ticket's latest merge, with a sha and not reverted. */
async function checkTarget(on: RevertTarget, fetch: Fetch): Promise<Checked> {
  try {
    const ticket = await ticketQuery(on.base, on.ticket, fetch).queryFn();
    if (ticket == null) return failure(`${on.ticket} is not mirrored on this host`);
    const latest = Math.max(-1, ...ticket.runs.filter((each) => each.outcome === "merged").map((each) => each.run));
    if (latest < 0) return failure(`${on.ticket} has no merged run`);
    const attempt = ticket.runs.reduce((top, each) => (each.run > top.run ? each : top), ticket.runs[0]!);
    if (on.newest === true && attempt.run !== latest) {
      return failure(`${on.ticket}'s newest attempt, run ${attempt.run}, did not merge; nothing was filed. Revert run ${latest} from its run page.`);
    }
    if (on.run != null && on.run !== latest) {
      return failure(`Run ${on.run} is not ${on.ticket}'s latest merge; a revert would undo run ${latest}. Revert it from run ${latest}.`);
    }
    if (ticket.reverted != null) return failure(`${on.ticket}'s merge was already reverted${ticket.reverted.by == null ? "" : ` by ${ticket.reverted.by}`}.`);
    const { merge_sha: sha, reverted } = (await fetchRoute(fetch, `${on.base}/runs/${latest}`, runDetailSchema)).run;
    if (reverted != null) return failure(`Run ${latest} of ${on.ticket} was already reverted${reverted.by == null ? "" : ` by ${reverted.by}`}.`);
    if (sha == null) {
      const older = ticket.runs.some((each) => each.outcome === "merged" && each.run !== latest);
      const outcome = older ? `a revert would undo an older merge of ${on.ticket}` : "there is no merge to revert";
      return failure(`Run ${latest} recorded no merge sha, so ${outcome}. Ship a forward fix instead.`);
    }
    return { sha };
  } catch (failed) {
    return failure(failed instanceof Error ? failed.message : String(failed));
  }
}

/** Every row's previews share one count, so only the newest asked opens. */
let newestPreview = 0;

function Refusal({ answer }: { answer: Extract<RevertAnswer, { kind: "refused" | "failed" }> }) {
  return (
    <div data-revert-refusal className="mt-3 rounded-[6px] bg-[color-mix(in_srgb,var(--bad)_10%,var(--raised))] px-3.5 py-3 font-sans text-[14px] leading-[1.5] text-ink shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--bad)_41.5%,var(--raised))]">
      {answer.kind === "refused" && answer.paths.length > 0 ? (
        <>
          It changed{" "}
          {answer.paths.map((path, index) => (
            <span key={path}>
              {index > 0 && ", "}
              <code className="font-mono text-[12.5px]">{path}</code>
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

function RevertDialog({ target, sha: merge, first, onClose, onFiled }: {
  target: RevertTarget; sha: string | null; first: RevertAnswer; onClose: () => void; onFiled: (ticket: string) => void;
}) {
  const deps = useContext(PollDepsContext);
  const file = useRevert();
  const [answer, setAnswer] = useState(first);
  const [reason, setReason] = useState("");
  const [failed, setFailed] = useState<string | null>(null);
  const reasonId = useId();
  const [checking, setChecking] = useState(false);
  const sha = shortSha(merge);
  const label = target.run == null ? target.project : `${target.project} · run ${target.run}`;
  if (answer.kind !== "preview") {
    return (
      <ConfirmDialog label={label} heading={`${target.ticket} can't be reverted`} cancelLabel="Close" onCancel={onClose}>
        {answer.kind !== "filed" && <Refusal answer={answer} />}
      </ConfirmDialog>
    );
  }
  const send = async () => {
    setChecking(true);
    const checked = await checkTarget(target, deps.fetch);
    setChecking(false);
    if ("refusal" in checked || checked.sha !== merge) {
      setAnswer("refusal" in checked ? checked.refusal : { kind: "failed", reason: `${target.ticket} merged again since this preview; nothing was filed. Preview its newest merge.` });
      return;
    }
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
      busy={checking || file.isPending}
      cancelLabel="Keep it"
      cancelDisabled={checking || file.isPending}
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
      <h3 className="m-0 mt-5 font-sans text-[13px] font-semibold leading-none text-ink">Merged since, same files</h3>
      {answer.dependents.length === 0 ? (
        <p className="m-0 mt-3.5 font-sans text-[13px] leading-[1.45] text-ink-3">Nothing merged since touches these files</p>
      ) : (
        <ul aria-label="Merged since, same files" className="m-0 mt-3.5 flex max-h-[min(220px,28vh)] list-none flex-col gap-1.5 overflow-y-auto p-0">
          {answer.dependents.map((dependent) => (
            <li key={`${dependent.key} ${dependent.sha}`} data-dependent className="flex flex-wrap gap-x-2.5 font-mono text-[12.5px] leading-[1.45] text-ink-2">
              <span className="font-semibold text-ink">{dependent.key}</span>
              <span>{shortSha(dependent.sha)}</span>
              <span className="min-w-0 [overflow-wrap:anywhere]">{dependent.paths.join(", ")}</span>
            </li>
          ))}
        </ul>
      )}
      <label htmlFor={reasonId} className="mt-5 block font-sans text-[13px] font-semibold leading-none text-ink">
        Reason
      </label>
      <textarea
        id={reasonId}
        value={reason}
        required
        onChange={(event) => setReason(event.target.value)}
        rows={3}
        placeholder="Why revert it?"
        className="field mt-3 block w-full resize-y leading-[1.5]"
      />
    </ConfirmDialog>
  );
}

/** `items` then Revert… for `target` unless it is null, and what it opens:
 *  the confirm after a preview, and once filed a status line naming the new
 *  ticket. Disabled once `reverted`, naming the revert ticket. */
export function useRevertFlow(target: RevertTarget | null, items: MenuItem[]): { items: MenuItem[]; layer: ReactNode } {
  const deps = useContext(PollDepsContext);
  const [previewing, setPreviewing] = useState(false);
  const [open, setOpen] = useState<{ target: RevertTarget; sha: string | null; answer: RevertAnswer; opener: HTMLElement | null } | null>(null);
  const [filed, setFiled] = useState<{ ticket: string; of: RevertTarget } | null>(null);
  const start = async (on: RevertTarget) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setFiled(null);
    setPreviewing(true);
    const asked = ++newestPreview;
    const checked = await checkTarget(on, deps.fetch);
    const answer = "refusal" in checked ? checked.refusal : await previewRevert(on.base, on.ticket, deps.fetch);
    setPreviewing(false);
    if (asked !== newestPreview || document.querySelector('[aria-modal="true"]') != null) return;
    setOpen({ target: on, sha: "sha" in checked ? checked.sha : null, answer, opener });
  };
  const close = () => {
    open?.opener?.focus();
    setOpen(null);
  };
  const reverted = target?.reverted;
  const all: MenuItem[] =
    target == null
      ? items
      : [
          ...items,
          {
            label: "Revert…",
            danger: true,
            divided: true,
            disabled: reverted != null || target.off != null || previewing,
            note: reverted != null ? (reverted.by == null ? "Reverted" : `Reverted by ${reverted.by}`) : (target.off ?? undefined),
            onSelect: () => void start(target),
          },
        ];
  const layer = (
    <>
      {open != null && (
        <RevertDialog
          target={open.target}
          sha={open.sha}
          first={open.answer}
          onClose={close}
          onFiled={(ticket) => {
            setFiled({ ticket, of: open.target });
            close();
          }}
        />
      )}
      {filed != null && (
        <p role="status" data-revert-filed className={`relative z-[2] m-0 text-ink-2 ${detailLine}`}>
          Filed{" "}
          <Link to="/tickets/$project/$key" params={{ project: filed.of.project, key: filed.ticket }} search={keepScope} className="font-medium text-ink underline underline-offset-2">
            {filed.ticket}
          </Link>{" "}
          to revert {filed.of.ticket}.
        </p>
      )}
    </>
  );
  return { items: all, layer };
}
