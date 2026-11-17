import type { ReactNode } from "react";
import type { CardLook } from "../state";

export type CardMark = "need" | "fail" | "wait";

const LOOKS: Record<CardLook, { surface: string; ring: string }> = {
  merged: { surface: "bg-raised", ring: "shadow-[inset_0_0_0_1px_var(--hair)]" },
  live: { surface: "bg-raised", ring: "shadow-[inset_0_0_0_1px_var(--live)]" },
  todo: { surface: "bg-raised", ring: "shadow-[inset_0_0_0_1px_var(--hair)]" },
  backlog: { surface: "border border-dashed border-hair-2 text-ink-3", ring: "" },
};

const MARKS: Record<CardMark, string> = {
  need: "shadow-[inset_0_0_0_1px_var(--need)]",
  fail: "shadow-[inset_0_0_0_1px_var(--bad)]",
  wait: "shadow-[inset_0_0_0_1px_var(--wait)]",
};

/** A look's surface and hairline with its mark, for a card or a swatch of one. */
export function cardSurface(look: CardLook, mark?: CardMark): string {
  return `${LOOKS[look].surface} ${mark == null ? LOOKS[look].ring : MARKS[mark]}`;
}

export function Card({ look, mark, children }: { look: CardLook; mark?: CardMark; children?: ReactNode }) {
  return (
    <article data-card={look} data-mark={mark} className={`flex flex-col gap-2 rounded-[10px] px-3.5 py-3 transition-colors duration-(--motion-colour) ${cardSurface(look, mark)}`}>
      {children}
    </article>
  );
}
