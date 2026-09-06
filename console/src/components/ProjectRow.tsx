/** One project in the rail: status dot, name, sub-line, run count on the right. */
export function ProjectRow({
  name,
  sub,
  count,
  tone,
  selected,
  onClick,
}: {
  name: string;
  sub?: string;
  count?: number;
  tone: "ok" | "bad" | "faint";
  selected: boolean;
  onClick: () => void;
}) {
  const dot = { ok: "bg-ok", bad: "bg-bad", faint: "bg-rail-faint" }[tone];
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-button p-2 text-left ${
        selected ? "bg-rail-selected" : "hover:bg-rail-card"
      }`}
    >
      <span aria-hidden="true" className={`size-[9px] shrink-0 rounded-chip ${dot}`} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold text-rail-text">{name}</span>
        {sub && <span className="block truncate text-[11px] text-rail-sub">{sub}</span>}
      </span>
      {count != null && <span className="font-mono text-[11px] text-rail-sub">{count}</span>}
    </button>
  );
}
