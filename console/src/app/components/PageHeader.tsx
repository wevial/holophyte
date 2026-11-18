import type { ReactNode } from "react";

/** A page's title row: the title, its quiet count, then `tools` at the right, with `tabs` under it. */
export function PageHeader({ title, count, tools, tabs }: { title: string; count?: { text: ReactNode; need?: string; after?: string }; tools?: ReactNode; tabs?: ReactNode }) {
  return (
    <header className="flex flex-col gap-3.5">
      <div className="flex min-h-[30px] flex-wrap items-center gap-x-3 gap-y-2">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] tracking-[-0.01em] text-ink">{title}</h1>
        {count != null && (
          <p data-page-count className="font-sans text-[13px] text-ink-3">
            {count.text}
            {count.need != null && (
              <>
                {" · "}
                <b className="font-medium text-need">{count.need}</b>
              </>
            )}
            {count.after != null && ` · ${count.after}`}
          </p>
        )}
        {tools != null && <div data-tools className="relative ml-auto flex max-w-full flex-wrap items-center gap-1 *:max-w-full *:flex-wrap">{tools}</div>}
      </div>
      {tabs}
    </header>
  );
}
