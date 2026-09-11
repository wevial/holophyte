import { plainReason } from "../lib/attention";
import type { AttentionItem } from "../lib/types";

/** A failed ticket's attempts under its row, oldest first: one line per
 *  run naming its number, its plain reason, and the branch and short sha
 *  the reason preserved. The raw reason stays in the run detail. */
export function AttemptsCard({ runs }: { runs: AttentionItem[] }) {
  return (
    <div
      data-attempts-card
      className="my-2 mb-3 ml-[110px] overflow-hidden rounded-[10px] border border-line bg-card"
    >
      <ol className="list-none">
        {runs.map((run, index) => {
          const plain = plainReason(typeof run.reason === "string" ? run.reason : "");
          const parts = [
            typeof run.run === "number" ? `run #${run.run}` : "run",
            plain.sentence,
            plain.branch != null && plain.sha != null ? `${plain.branch} @ ${plain.sha.slice(0, 7)}` : null,
          ].filter((part): part is string => part != null);
          return (
            <li
              key={`${String(run.run ?? index)}`}
              data-attempt
              className={`px-[14px] py-[10px] font-mono text-[12px] text-body ${index > 0 ? "border-t border-line-faint" : ""}`}
            >
              {parts.join(" · ")}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
