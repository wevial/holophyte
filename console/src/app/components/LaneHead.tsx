import type { ReactNode } from "react";

/** A project lane's tab: its name, the held tag and note, its meta, then
 *  whatever the lane adds after them. Attached, it opens onto the box after it. */
export function LaneHead({
  name,
  held,
  holdNote,
  meta,
  attached = true,
  children,
}: {
  name: string;
  held: boolean;
  holdNote?: string | null;
  meta: string;
  attached?: boolean;
  children?: ReactNode;
}) {
  const edge = attached ? "border border-b-0 border-rule bg-paper -mb-px" : "border border-rule bg-paper";
  return (
    <div
      data-lane-head
      className={`${edge} relative z-[1] flex w-fit max-w-full flex-wrap items-center gap-x-3.5 gap-y-1 px-3.5 py-2 font-mono text-[12px] leading-[1.3] text-ink-3`}
    >
      <span className="font-sans text-[13px] font-semibold text-ink">{name}</span>
      {held && <span className="text-wait">held{holdNote ? ` · ${holdNote}` : ""}</span>}
      <span>{meta}</span>
      {children}
    </div>
  );
}
