/** The kind pill: questions on the warn wash, everything else on the bad one. */
export function KindPill({ kind, children }: { kind: string; children: string }) {
  const tone = kind === "blocked" ? "bg-warn-bg text-warn-text" : "bg-bad-bg text-bad-text";
  return (
    <span data-kind={kind} className={`inline-block rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${tone}`}>
      {children}
    </span>
  );
}
