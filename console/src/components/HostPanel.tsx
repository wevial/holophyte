import { useState } from "react";
import { CHIP_LABELS, KINDS, countsByKind, type Kind } from "../lib/attention";
import { projectName } from "../lib/derive";
import { age } from "../lib/format";
import { addressOf, hostName, hostTone, runCounts, supervisorStale, toilLines, type HostRecord } from "../lib/hosts";
import { routeParts, routeText } from "../lib/routes";
import { forgetToken, storeToken, tokenFor } from "../lib/token";
import { ActionButton } from "./ActionButton";
import { TOKEN_REJECTED } from "./HostRow";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** Who wrote a supervisor beat: `pid N`, or "host sweep" for pid 0, the
 *  host sweep's one sentinel row per store (no process to name); null
 *  when the beat names none. */
export function beatPid(pid: number | null): string | null {
  if (pid == null) return null;
  return pid === 0 ? "host sweep" : `pid ${pid}`;
}

/** "1 question · 2 stale runs" from a host's own `/attention`, empty with none. */
const NOUNS: Record<Kind, string> = {
  blocked: "question",
  pr_open: "open PR",
  paused: "paused ticket",
  stale_run: "stale run",
  failed: "failed run",
  supervisor: "supervisor",
  unreachable: "unreachable",
};

function attentionSummary(host: HostRecord): string[] {
  const counts = countsByKind(host.attention?.items ?? []);
  return KINDS.filter((kind) => counts[kind] > 0).map((kind) => plural(counts[kind], NOUNS[kind] ?? CHIP_LABELS[kind]));
}

/** The field a daemon that answered 401 gets in place of its cells: a
 *  password input and the one enabled button on the page, "Use", which
 *  writes to the browser's storage, not to the daemon. The next poll
 *  carries the token; until it answers, the field is folded away and the
 *  card says so, and if that poll is 401 again the field is back with
 *  the token forgotten. A value the header could not carry is refused at
 *  the field, with the reason under it and nothing stored. `onStored`
 *  tells the card a value was kept, so its Forget button appears at
 *  once rather than on the next poll. */
export function TokenField({ host, onStored }: { host: HostRecord; onStored: () => void }) {
  const [token, setToken] = useState("");
  const [reason, setReason] = useState<string | null>(null);
  const [sentAt, setSentAt] = useState<number | null>(null);
  if (sentAt === host.polled_ms) {
    return (
      <p data-token-sent className="mt-4 text-[13px] text-muted">
        token stored · polling
      </p>
    );
  }
  const id = `token-${host.address}`;
  return (
    <form
      data-token-form
      className="mt-4 flex items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        // Keyed by the request address, the same key the fetch seam reads
        // (`tokenedFetch`), which for the origin can differ from the
        // address the daemon advertises as `/peers.self`.
        const refused = storeToken(addressOf(host.base), token);
        setReason(refused);
        if (refused != null) return;
        setToken("");
        setSentAt(host.polled_ms);
        onStored();
      }}
    >
      <div className="flex flex-1 flex-col gap-1">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Token</span>
          <input
            id={id}
            name="token"
            type="password"
            autoComplete="off"
            aria-describedby={reason == null ? undefined : `${id}-reason`}
            value={token}
            onChange={(event) => setToken(event.currentTarget.value)}
            className="rounded-button border border-chip-border bg-card px-2 py-1 font-mono text-[13px] text-ink"
          />
        </label>
        {reason != null && (
          <span id={`${id}-reason`} data-token-reason className="text-[12px] text-bad-text">
            {reason}
          </span>
        )}
      </div>
      <button
        type="submit"
        className="rounded-button border border-chip-border px-2 py-1 text-[12px] font-semibold text-ink"
      >
        Use
      </button>
    </form>
  );
}

/** One daemon's card in the Hosts view: the dot, name and address, the
 *  daemon, supervisor and runs cells, a toil cell when the daemon sends
 *  one, its project row, and the two
 *  operator actions, rendered
 *  disabled until writes arrive. An unreachable daemon
 *  says so in the header, with its last good answer's age, in place of the
 *  three cells; one that answered 401 gets the token field there instead.
 *  `now` is the console's clock. */
export function HostPanel({ host, now }: { host: HostRecord; now: number }) {
  const { status } = host;
  // Bumped when the field stores a token, so the Forget button appears
  // at once rather than on the next poll.
  const [, rerender] = useState(0);
  // Bumped when the card forgets its token: keys the field, so forgetting
  // while the card waits on that poll brings the field straight back.
  const [fieldKey, setFieldKey] = useState(0);
  const unreachable = host.error != null;
  const needsToken = host.needs_token;
  // The key the fetch seam reads (`tokenedFetch`), which for the origin
  // can differ from the address the daemon advertises as `/peers.self`.
  const tokenAddress = addressOf(host.base);
  // Whenever a value is stored, including one just submitted while the
  // card still shows 401 from the poll before it.
  const hasToken = tokenFor(tokenAddress) != null;
  const tone = hostTone(host);
  const dot = { ok: "bg-ok", bad: "bg-bad", faint: "bg-faint" }[tone];
  const stale = status ? supervisorStale(host.name != null, status) : false;
  const seen = host.seen_ms == null ? "never answered" : `last seen ${age(now - host.seen_ms)} ago`;
  const counts = status ? runCounts(status) : null;
  const runsCell = counts
    ? [counts.active > 0 || counts.stale === 0 ? `${counts.active} active` : null, counts.stale > 0 ? `${counts.stale} stale` : null]
        .filter((part): part is string => part != null)
        .join(" · ")
    : "";
  const supervisorCell = status
    ? [
        stale ? "stale" : status.supervisor.state,
        beatPid(status.supervisor.pid),
        status.supervisor.heartbeat_age_ms == null ? null : `hb ${age(status.supervisor.heartbeat_age_ms)}`,
      ]
        .filter((part): part is string => part != null)
        .join(" · ")
    : "";
  const toil = status?.toil ? toilLines(status.toil) : null;
  const summary = [status ? plural(status.runs.length, "run") : null, ...attentionSummary(host)]
    .filter((part): part is string => part != null)
    .join(" · ");

  return (
    <article
      aria-label={hostName(host)}
      data-host={host.address}
      data-unreachable={unreachable || undefined}
      data-needs-token={needsToken || undefined}
      data-stale={(stale && !unreachable) || undefined}
      className={`rounded-[10px] border bg-card p-[18px] shadow-card ${tone === "bad" ? "border-bad/50" : "border-line"}`}
    >
      <header className="flex items-center gap-2.5">
        <span aria-hidden="true" className={`size-2.5 shrink-0 rounded-chip ${dot}`} />
        <span className="text-[17px] font-semibold text-ink">{hostName(host)}</span>
        <span className="truncate font-mono text-[12px] text-muted">{host.address}</span>
        {unreachable && (
          <span data-unreachable-line className="ml-auto font-mono text-[12px] font-semibold text-bad">
            unreachable · {seen}
          </span>
        )}
        {needsToken && (
          <span data-needs-token-line className="ml-auto font-mono text-[12px] text-muted">
            {host.token_rejected ? TOKEN_REJECTED : "needs token"}
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
      {needsToken && <TokenField key={fieldKey} host={host} onStored={() => rerender((count) => count + 1)} />}
      {!unreachable && !needsToken && status && (
        <dl className={`mt-4 grid gap-4 ${toil ? "grid-cols-4" : "grid-cols-3"}`}>
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Daemon</dt>
            <dd data-daemon className="mt-1 text-[13px] text-body">
              {status.daemon ? `up ${age(status.now - status.daemon.started_ms)}` : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Supervisor</dt>
            <dd
              data-supervisor={stale ? "stale" : status.supervisor.state}
              className={`mt-1 font-mono text-[13px] ${stale ? "font-semibold text-bad-text" : "text-ok-text"}`}
            >
              {supervisorCell}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Runs</dt>
            <dd data-runs className="mt-1 text-[13px] text-body">
              {runsCell}
            </dd>
          </div>
          {toil && (
            <div>
              <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Toil</dt>
              <dd data-toil className="mt-1 text-[13px] text-body">
                <span data-toil-rates className="block">{toil.rates}</span>
                {toil.actions != null && (
                  <span data-toil-actions className="block text-[12px] text-muted">
                    {toil.actions}
                  </span>
                )}
              </dd>
            </div>
          )}
        </dl>
      )}
      {status && host.project != null && (
        <div className="mt-4 border-t border-line-faint pt-3">
          <p className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">Projects</p>
          <div className="mt-1.5 flex items-baseline gap-2">
            <span className="text-[13px] font-semibold text-ink">{projectName(host.project)}</span>
            <span className="truncate font-mono text-[12px] text-muted">{host.project}</span>
            <span data-project-summary className="ml-auto shrink-0 text-[12px] text-muted">
              {summary}
            </span>
          </div>
        </div>
      )}
      <footer className="mt-4 flex gap-2">
        <ActionButton>Restart supervisor</ActionButton>
        <ActionButton>Open daemon log</ActionButton>
      </footer>
    </article>
  );
}

const SEATS = [
  ["implementer", "Implementer"],
  ["reviewer", "Reviewer"],
  ["adjudicator", "Adjudicator"],
  ["writer", "Writer"],
] as const;

const HEADING = "text-[11px] font-semibold uppercase tracking-[.08em] text-faint";

/** One route as its harness in the body colour with the model muted
 *  beside it; an em dash for a seat with no route. */
function Route({ label }: { label: string | null | undefined }) {
  if (label == null) return <span className="text-muted">—</span>;
  const { harness, model } = routeParts(label);
  return (
    <span>
      <span className="text-body">{harness}</span>
      {model && <> <span className="text-muted">{model}</span></>}
    </span>
  );
}

/** A seat's cell: its route, the reviewer's fallback on a muted second
 *  line, and a writer that follows the implementer said as much. */
function SeatCell({ seat, labels }: { seat: (typeof SEATS)[number][0]; labels: Record<string, string | null> }) {
  const label = labels[seat];
  if (seat === "writer" && label != null && label === labels.implementer) {
    return <span className="text-muted">same as implementer</span>;
  }
  const fallback = seat === "reviewer" ? labels.reviewer_fallback : null;
  return (
    <>
      <Route label={label} />
      {fallback != null && (
        <span data-fallback className="block text-[12px] text-muted">
          fallback {routeText(fallback)}
        </span>
      )}
    </>
  );
}

/** One host's Agents table: a row per project its daemons serve, by name,
 *  and a column per seat, from each daemon's last good `route_labels`. A
 *  daemon that never sent them has no row; a host with no rows, no table. */
export function HostAgents({ label, hosts }: { label: string; hosts: HostRecord[] }) {
  const rows = hosts.flatMap((host) =>
    host.project != null && host.status?.route_labels ? [{ host, project: host.project, labels: host.status.route_labels }] : [],
  );
  if (rows.length === 0) return null;
  return (
    <section className="col-span-2 rounded-[10px] border border-line bg-card px-[18px] py-3 shadow-card">
      <p className={HEADING}>Agents · {label}</p>
      <table aria-label={`${label} agents`} className="mt-1.5 w-full text-left text-[13px]">
        <thead>
          <tr>
            <th scope="col" className={`py-1.5 pr-3 ${HEADING}`}>Project</th>
            {SEATS.map(([seat, heading]) => (
              <th key={seat} scope="col" className={`py-1.5 pr-3 ${HEADING}`}>
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ host, project, labels }) => (
            <tr key={host.key} className="border-t border-line-faint align-top">
              <th scope="row" className="py-2 pr-3 font-semibold text-ink">
                {projectName(project)}
              </th>
              {SEATS.map(([seat]) => (
                <td key={seat} data-seat={seat} className="py-2 pr-3">
                  <SeatCell seat={seat} labels={labels} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
