import type { ReactNode } from "react";

/** A labelled group in the rail: uppercase 11px label over its rows. */
export function RailGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section aria-label={label} className="flex flex-col gap-0.5">
      <h2 className="mb-1.5 px-2 text-[11px] font-semibold uppercase tracking-[.08em] text-rail-faint">
        {label}
      </h2>
      {children}
    </section>
  );
}
