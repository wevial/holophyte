import type { RunCriterion } from "./contract.gen";

/** `open` is not yet witnessed; `todo` has no run to witness it. */
export type Standing = "met" | "unmet" | "open" | "todo";

export interface CriterionRow {
  id: string;
  text: string;
  standing: Standing;
}

const STANDING: Record<RunCriterion["status"], Standing> = { met: "met", not_met: "unmet", unwitnessed: "open" };

export function criteriaRows(criteria: { id: string; text: string }[], judged: Pick<RunCriterion, "id" | "status">[], hasRun: boolean): CriterionRow[] {
  const byId = new Map(judged.map((entry) => [entry.id, entry.status]));
  return criteria.map(({ id, text }) => {
    const status = byId.get(id);
    return { id, text, standing: status != null ? STANDING[status] : hasRun ? "open" : "todo" };
  });
}
