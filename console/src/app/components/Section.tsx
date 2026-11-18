import type { ReactNode } from "react";
import { FactIcon } from "./icons";

export function Section({ label, count, aside, empty, footer, children }: { label: string; count?: number; aside?: ReactNode; empty: string; footer?: ReactNode; children?: ReactNode }) {
  return (
    <section aria-label={label} className="flex flex-col">
      <header className="flex h-[30px] items-center gap-2.5 border-b border-hair px-2.5">
        <h2 className="font-sans text-[13px] font-semibold text-ink">{label}</h2>
        {count != null && <span data-count className="font-mono text-[12px] text-ink-3">{count}</span>}
        {aside != null && <span className="ml-auto">{aside}</span>}
      </header>
      {count == null ? null : count === 0 ? <p className="flex items-center gap-2 px-2.5 py-2.5 font-sans text-[13px] text-ink-3"><FactIcon ok />{empty}</p> : children}
      {count != null && footer}
    </section>
  );
}
