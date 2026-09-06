import type { ReactNode } from "react";

/** The reason every action is inert until the write ticket lands. */
export const WRITES_LATER = "Writes arrive later behind a token";

/** A row action. This ticket ships no write, so it is always `disabled`. */
export function ActionButton({ children }: { children: ReactNode }) {
  return (
    <button
      type="button"
      disabled
      title={WRITES_LATER}
      className="rounded-button border border-chip-border px-2 py-1 text-[12px] font-semibold text-muted opacity-60"
    >
      {children}
    </button>
  );
}
