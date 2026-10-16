import { useState } from "react";
import { formatClock } from "../../lib/format";
import type { ProgressKind, ProgressLine } from "../progress";
import { SECTION_LABEL } from "./Criteria";
import { LoopIcon } from "./icons";
import { StateDot } from "./StateDot";

const KIND_TEXT: Record<ProgressKind, string> = {
  event: "text-ink-2",
  loop: "text-ink",
  round: "text-ink-2",
  steer: "text-act",
  ask: "text-ink-2",
  merge: "text-live",
  failure: "text-bad",
};

const lineKey = (line: ProgressLine) => `${line.at}:${line.kind}:${line.text}`;

/** The run's Progress: one line each, oldest first. A line that was not
 *  there at first render is marked new and fades in. */
export function ProgressLog({ lines, live }: { lines: ProgressLine[]; live: boolean }) {
  const [seen] = useState(() => new Set(lines.map(lineKey)));
  const counts = new Map<string, number>();
  return (
    <section id="progress" aria-label="Progress" className="scroll-mt-4">
      <h2 className={`m-0 ${SECTION_LABEL}`}>
        Progress
        {live && (
          <span data-live className="inline-flex items-center gap-1.5 tracking-[.1em] text-live">
            <span aria-hidden="true">
              <StateDot state="running" hideWord tone="live" />
            </span>
            live
          </span>
        )}
      </h2>
      <ol aria-live="polite" className="m-0 mt-3 list-none p-0 font-mono text-[12.5px] leading-[1.5]">
        {lines.map((line) => {
          const key = lineKey(line);
          const repeat = counts.get(key) ?? 0;
          counts.set(key, repeat + 1);
          const fresh = !seen.has(key);
          return (
            <li
              key={`${key}:${repeat}`}
              data-kind={line.kind}
              data-new={fresh ? "" : undefined}
              className={`grid grid-cols-[44px_minmax(0,1fr)] gap-x-2.5 border-b border-dashed border-rule-2 py-[5px] ${fresh ? "log-new" : ""}`}
            >
              <time dateTime={new Date(line.at).toISOString()} className="text-ink-3">
                {formatClock(line.at)}
              </time>
              <span data-line-text className={`inline-flex items-center gap-2 [overflow-wrap:anywhere] ${KIND_TEXT[line.kind]}`}>
                {line.kind === "loop" && <LoopIcon />}
                <span className="min-w-0">{line.text}</span>
              </span>
              {line.sub?.map((sub, index) => (
                <span key={index} data-sub className="col-start-2 pl-3.5 text-ink-3 [overflow-wrap:anywhere]">
                  {sub}
                </span>
              ))}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
