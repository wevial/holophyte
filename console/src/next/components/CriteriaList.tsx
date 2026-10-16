import type { RunDetail } from "../../lib/contract.gen";
import { SECTION_LABEL } from "./Criteria";
import { CriterionMark } from "./icons";

export function CriteriaList({ detail, live }: { detail: RunDetail; live: boolean }) {
  const { criteria, rounds, run } = detail;
  const met = criteria.filter((criterion) => criterion.status === "met").length;
  const active = rounds.find((round) => round.ended_ms == null);
  const pending = active?.round ?? Math.max(0, ...rounds.map((round) => round.round)) + 1;
  const notes = criteria.filter((criterion) => criterion.note.trim());
  const tests = notes.filter((criterion) => criterion.status === "met").length;
  const findings = notes.length - tests;
  const evidence = [tests ? `${tests} test${tests === 1 ? "" : "s"}` : "", findings ? `${findings} finding${findings === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
  return (
    <section aria-label="Acceptance criteria">
      <div className={SECTION_LABEL}>
        Acceptance criteria
        {criteria.length > 0 && <span className="font-mono font-normal normal-case tracking-normal">{met} of {criteria.length} met</span>}
      </div>
      {criteria.length === 0 ? (
        <p className="mb-0 mt-3 font-mono text-[12px] text-ink-3">No review has judged the criteria yet.</p>
      ) : (
        <>
          <ol className="m-0 mt-3 list-none border-t border-rule-2 p-0">
            {criteria.map((criterion) => {
              const waiting = live && (criterion.run !== run.id || (criterion.status !== "met" && active != null && criterion.round < active.round));
              return (
                <li key={criterion.n} className="flex flex-wrap items-center gap-3 border-b border-rule-2 py-3">
                  <CriterionMark status={criterion.status} />
                  <span className="w-[34px] shrink-0 font-mono text-[11px] font-semibold text-ink-3">AC{criterion.n}</span>
                  <span className="min-w-0 flex-1 font-plex text-[14px] text-ink-2">{criterion.text}</span>
                  {waiting && <span className="font-mono text-[11px] text-ink-3">waits on round {pending}</span>}
                </li>
              );
            })}
          </ol>
          <details className="mt-3.5 border-t border-rule">
            <summary className="cursor-pointer py-3 font-mono text-[11px] font-medium text-ink-2">
              Evidence{evidence && <> <span className="ml-2.5 text-ink-3">{evidence}</span></>}
            </summary>
            <ul className="m-0 list-none p-0 font-mono text-[12px] leading-[1.8] text-ink-2 [overflow-wrap:anywhere]">
              {notes.map((criterion) => (
                <li key={criterion.n}>AC{criterion.n} · {criterion.status !== "met" && `round ${criterion.round}: `}{criterion.note}</li>
              ))}
            </ul>
          </details>
        </>
      )}
    </section>
  );
}
