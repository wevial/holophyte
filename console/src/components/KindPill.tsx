/** The kind pill: questions and open PRs on the warn wash, a neutral `step` (a
 *  resolution the ledger did not classify) on the paper, everything else
 *  on the bad wash. */
export function KindPill({ kind, children }: { kind: string; children: string }) {
  const tone =
    kind === "blocked" || kind === "pr_open"
      ? "bg-warn-bg text-warn-text"
      : kind === "step"
        ? "bg-paper text-muted"
        : "bg-bad-bg text-bad-text";
  return (
    <span data-kind={kind} className={`inline-block rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${tone}`}>
      {children}
    </span>
  );
}
