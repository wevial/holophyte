import type { RunCriterion } from "./contract.gen";

/** `open` is not yet witnessed; `todo` has no run to witness it. */
export type Standing = "met" | "unmet" | "open" | "todo";

export interface CriterionRow {
  n: number;
  text: string;
  standing: Standing;
}

const STANDING: Record<RunCriterion["status"], Standing> = { met: "met", not_met: "unmet", unwitnessed: "open" };

/** One row per criterion text, numbered from 1, its standing from the
 *  `judged` entry with the same `n`. */
export function criteriaRows(texts: string[], judged: Pick<RunCriterion, "n" | "status">[], hasRun: boolean): CriterionRow[] {
  const byN = new Map(judged.map((entry) => [entry.n, entry.status]));
  return texts.map((text, index) => {
    const status = byN.get(index + 1);
    return { n: index + 1, text, standing: status != null ? STANDING[status] : hasRun ? "open" : "todo" };
  });
}
