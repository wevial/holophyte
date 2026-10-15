import type { ReactNode } from "react";
import type { CardLook } from "../state";

const LOOKS: Record<CardLook, { card: string; title: string }> = {
  merged: { card: "border border-rule-2 bg-live-wash/50", title: "text-[14px] font-medium text-ink-2" },
  live: { card: "border-2 border-live bg-live-wash", title: "text-[15px] font-semibold text-ink" },
  todo: { card: "border border-rule bg-paper", title: "text-[15px] font-semibold text-ink" },
  backlog: { card: "border border-dashed border-rule text-ink-3", title: "text-[15px] font-medium text-ink-3" },
};

export function Card({ look, title, children }: { look: CardLook; title?: ReactNode; children?: ReactNode }) {
  return (
    <article data-card={look} className={`flex flex-col gap-1.5 px-4 py-3 ${LOOKS[look].card}`}>
      {title != null && <h3 className={`font-sans ${LOOKS[look].title}`}>{title}</h3>}
      {children}
    </article>
  );
}
