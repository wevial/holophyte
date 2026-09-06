/** A filter chip with its count; the selected one inverts to ink on paper. */
export function Chip({
  label,
  count,
  selected,
  onClick,
}: {
  label: string;
  count: number;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={`rounded-chip px-[10px] py-[5px] text-[12px] font-semibold ${
        selected ? "bg-ink text-paper" : "border border-chip-border bg-transparent text-muted"
      }`}
    >
      {label} {count}
    </button>
  );
}
