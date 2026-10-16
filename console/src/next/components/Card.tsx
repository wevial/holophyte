import type { ReactNode } from "react";
import type { CardLook } from "../state";

export type CardMark = "need" | "fail" | "wait";

const LOOKS: Record<CardLook, { card: string; title: string }> = {
  merged: { card: "border border-rule-2 bg-live-wash/50", title: "text-[14px] font-medium text-ink-2" },
  live: { card: "border-2 border-live bg-live-wash", title: "text-[15px] font-semibold text-ink" },
  todo: { card: "border border-rule bg-paper", title: "text-[15px] font-semibold text-ink" },
  backlog: { card: "border border-dashed border-rule text-ink-3", title: "text-[15px] font-medium text-ink-3" },
};

const MARKS: Record<CardMark, string> = {
  need: "shadow-[inset_2px_0_0_var(--need)]",
  fail: "border-bad! shadow-[inset_2px_0_0_var(--need)]",
  wait: "border-wait!",
};

/** A look's surface and border with its mark, for a card or a swatch of one. */
export function cardSurface(look: CardLook, mark?: CardMark): string {
  return `${LOOKS[look].card} ${mark == null ? "" : MARKS[mark]}`;
}

export function Card({ look, mark, title, children }: { look: CardLook; mark?: CardMark; title?: ReactNode; children?: ReactNode }) {
  return (
    <article data-card={look} data-mark={mark} className={`flex flex-col gap-1.5 px-4 py-3 ${cardSurface(look, mark)}`}>
      {title != null && <h3 className={`font-sans ${LOOKS[look].title}`}>{title}</h3>}
      {children}
    </article>
  );
}
