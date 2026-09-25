import { useState } from "react";
import { ACTIONS_OFF, ROUTES, postAction } from "../lib/actions";
import { RUN_SWEEP } from "../lib/attention";
import { age } from "../lib/format";
import { SWEEP_OK, addressOf, hostName, rootOf, type HostRecord } from "../lib/hosts";
import type { Fetch } from "../lib/poll";
import { forgetToken, tokenFor } from "../lib/token";
import type { HostProject, HostStatus, Sweep } from "../lib/types";
import { ActionButton } from "./ActionButton";
import { TOKEN_REJECTED } from "./HostRow";
import { TokenField, beatPid } from "./HostPanel";

const HEADING = "text-[11px] font-semibold uppercase tracking-[.08em] text-faint";
const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
const pageFetch: Fetch = (url, init) => globalThis.fetch(url, init);

/** The Sweep cell: the daemon's word for the last run, when it ended
 *  against the daemon's clock, and its exit when it is not 0. */
export function sweepLine(sweep: Sweep, now: number): string {
  const ended = sweep.ended == null ? null : `ended ${age(now - sweep.ended)} ago`;
  const started = sweep.state === "running" && sweep.started != null ? `started ${age(now - sweep.started)} ago` : null;
  const exit = sweep.exit != null && sweep.exit !== 0 ? `exit ${sweep.exit}` : null;
  return [sweep.state, started ?? ended, exit].filter((part): part is string => part != null).join(" · ");
}

/** The Build cell's revisions, seven characters each, and whether the
 *  daemon's, the last sweep's and the checkout's differ (`/status`
 *  `build`); a revision nobody reported is left out of the comparison. */
export function buildLine(build: HostStatus["build"]): { text: string; differ: boolean } {
  const parts = (["daemon", "sweep", "head"] as const).map((key) => [key, build[key]] as const);
  const known = new Set(parts.flatMap(([, revision]) => (revision == null ? [] : [revision])));
  return {
    text: parts.map(([key, revision]) => `${key} ${revision == null ? "—" : revision.slice(0, 7)}`).join(" · "),
    differ: known.size > 1,
  };
}

/** One project's Beat cell: the store's beat as the root judged it. */
function beatLine(project: HostProject): string {
  const beat = project.supervisor;
  if (beat == null) return "—";
  return [beat.state, beatPid(beat.pid), beat.heartbeat_age_ms == null ? null : `hb ${age(beat.heartbeat_age_ms)}`]
    .filter((part): part is string => part != null)
    .join(" · ");
}

/** One host daemon's card in the Hosts view: every project the registry
 *  holds, from the daemon's own root `/status`, with the last sweep and
 *  the three builds. A project that cannot be read is its own row's
 *  error; the others read whole. The daemon's token is one for every
 *  project, asked for once here. `records` are the daemon's project
 *  records, in registry order; `now` is the console's clock. */
export function HostDaemonPanel({ records, now, actionFetch = pageFetch }: { records: HostRecord[]; now: number; actionFetch?: Fetch }) {
  const [, rerender] = useState(0);
  const [fieldKey, setFieldKey] = useState(0);
  const [ran, setRan] = useState<{ text: string; ok: boolean } | null>(null);
  const first = records[0]!;
  const root = first.host_status ?? null;
  const tokenAddress = addressOf(first.base);
  const hasToken = tokenFor(tokenAddress) != null;
  const down = first.root_failed === true && first.error != null;
  const needsToken = first.needs_token;
  const failing = root?.projects.filter((project) => project.error != null).length ?? 0;
  const sweepBad = root != null && !SWEEP_OK.has(root.sweep.state);
  const tone = down || failing > 0 || sweepBad ? "bad" : needsToken || root == null ? "faint" : "ok";
  const dot = { ok: "bg-ok", bad: "bg-bad", faint: "bg-faint" }[tone];
  const seen = first.seen_ms == null ? "never answered" : `last seen ${age(now - first.seen_ms)} ago`;
  const build = root ? buildLine(root.build) : null;
  const runSweep =
    root?.actions === true
      ? async () => {
          setRan(null);
          const result = await postAction(rootOf(first.base), ROUTES[RUN_SWEEP]!, {}, actionFetch);
          setRan({ text: result.detail, ok: result.ok });
        }
      : undefined;

  return (
    <article
      aria-label={hostName(first)}
      data-host-daemon={first.address}
      data-unreachable={down || undefined}
      data-needs-token={needsToken || undefined}
      className={`col-span-2 rounded-[10px] border bg-card p-[18px] shadow-card ${tone === "bad" ? "border-bad/50" : "border-line"}`}
    >
      <header className="flex items-center gap-2.5">
        <span aria-hidden="true" className={`size-2.5 shrink-0 rounded-chip ${dot}`} />
        <span className="text-[17px] font-semibold text-ink">{hostName(first)}</span>
        <span className="truncate font-mono text-[12px] text-muted">{first.address}</span>
        <span className="font-mono text-[12px] text-muted">{plural(root?.projects.length ?? records.length, "project")}</span>
        {down && (
          <span data-unreachable-line className="ml-auto font-mono text-[12px] font-semibold text-bad">
            unreachable · {seen}
          </span>
        )}
        {needsToken && (
          <span data-needs-token-line className="ml-auto font-mono text-[12px] text-muted">
            {first.token_rejected ? TOKEN_REJECTED : "needs token"}
          </span>
        )}
        {hasToken && (
          <button
            type="button"
            data-forget-token
            onClick={() => {
              forgetToken(tokenAddress);
              setFieldKey((key) => key + 1);
            }}
            className="ml-auto rounded-button border border-chip-border px-2 py-1 text-[12px] font-semibold text-ink"
          >
            Forget token
          </button>
        )}
      </header>
      {needsToken && <TokenField key={fieldKey} host={first} onStored={() => rerender((count) => count + 1)} />}
      {root && !needsToken && (
        <>
          <dl className="mt-4 grid grid-cols-3 gap-4">
            <div>
              <dt className={HEADING}>Daemon</dt>
              <dd data-daemon className="mt-1 text-[13px] text-body">
                {root.daemon ? `up ${age(root.now - root.daemon.started_ms)}` : "—"}
              </dd>
            </div>
            <div>
              <dt className={HEADING}>Sweep</dt>
              <dd
                data-sweep={root.sweep.state}
                className={`mt-1 font-mono text-[13px] ${sweepBad ? "font-semibold text-bad-text" : "text-ok-text"}`}
              >
                {sweepLine(root.sweep, root.now)}
              </dd>
            </div>
            <div>
              <dt className={HEADING}>Build</dt>
              <dd
                data-build
                data-build-differ={build!.differ || undefined}
                className={`mt-1 font-mono text-[12px] ${build!.differ ? "font-semibold text-bad-text" : "text-body"}`}
              >
                {build!.text}
              </dd>
            </div>
          </dl>
          <table aria-label={`${hostName(first)} projects`} className="mt-4 w-full text-left text-[13px]">
            <thead>
              <tr>
                {["Project", "Beat", "Runs", "Admission"].map((heading) => (
                  <th key={heading} scope="col" className={`py-1.5 pr-3 ${HEADING}`}>
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {root.projects.map((project) => (
                <tr key={project.path} data-project-row={project.name ?? project.path} className="border-t border-line-faint align-top">
                  <th scope="row" className="py-2 pr-3 font-semibold text-ink">
                    {project.name ?? project.path}
                    {project.error != null && (
                      <span data-project-error className="block font-mono text-[11px] font-normal text-bad-text">
                        {project.error}
                      </span>
                    )}
                  </th>
                  <td data-beat className={`py-2 pr-3 font-mono text-[12px] ${project.supervisor?.state === "stale" ? "text-bad-text" : "text-body"}`}>
                    {beatLine(project)}
                  </td>
                  <td data-project-runs className="py-2 pr-3 text-body">
                    {project.error != null ? "—" : project.runs.length}
                  </td>
                  <td className="py-2 pr-3 text-body">{project.admission ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <footer className="mt-4 flex flex-col items-start gap-1.5">
        <ActionButton onAct={runSweep} title={runSweep ? undefined : ACTIONS_OFF}>
          {RUN_SWEEP}
        </ActionButton>
        {ran && (
          <p data-action-detail data-ok={ran.ok} role="status" className={`text-[12px] ${ran.ok ? "text-muted" : "text-needs-you-link"}`}>
            {ran.text}
          </p>
        )}
      </footer>
    </article>
  );
}
