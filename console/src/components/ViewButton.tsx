import type { ReactNode } from "react";

/** A rail button: 13px/600, padding 8px, the selected one on a light wash. */
export function ViewButton({
  selected,
  onClick,
  badge,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  badge?: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={`flex w-full items-center justify-between rounded-button p-2 text-left text-[13px] font-semibold ${
        selected ? "bg-rail-selected text-rail-fg" : "text-rail-text hover:bg-rail-card"
      }`}
    >
      <span>{children}</span>
      {badge}
    </button>
  );
}
