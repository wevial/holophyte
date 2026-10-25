import type { ReactNode } from "react";

/** A project lane's caption: its name, the held tag and note, its meta,
 *  then whatever the lane adds after them. */
export function LaneHead({
  name,
  held,
  holdNote,
  meta,
  children,
}: {
  name: string;
  held: boolean;
  holdNote?: string | null;
  meta: string;
  children?: ReactNode;
}) {
  return (
    <div data-lane-head className="mb-2 flex flex-wrap items-center gap-x-3.5 gap-y-1 px-0.5 font-mono text-[12px] leading-[1.3] text-ink-3">
      <span className="font-sans text-[11.5px] font-semibold uppercase tracking-[0.1em] text-ink-2">{name}</span>
      {held && <span className="text-wait">held{holdNote ? ` · ${holdNote}` : ""}</span>}
      <span>{meta}</span>
      {children}
    </div>
  );
}
