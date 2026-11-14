import type { ReactNode } from "react";

export function Section({ label, count, aside, empty, footer, children }: { label: string; count?: number; aside?: ReactNode; empty: string; footer?: ReactNode; children?: ReactNode }) {
  return (
    <section aria-label={label} className="flex flex-col">
      <header className="flex min-h-8 items-baseline gap-2 border-b border-rule-2 px-2.5 pb-2">
        <h2 className="font-sans text-[14px] font-semibold text-ink">{label}</h2>
        {count != null && <span data-count className="font-mono text-[12px] text-ink-3">{count}</span>}
        {aside != null && <span className="ml-auto self-center">{aside}</span>}
      </header>
      {count == null ? null : count === 0 ? <p className="px-2.5 py-2.5 font-mono text-[12px] text-ink-3">{empty}</p> : children}
      {count != null && footer}
    </section>
  );
}
