import type { TicketSteer } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { SECTION_LABEL } from "./Criteria";
import { startedLabel } from "./TicketRuns";

function headOf(steer: TicketSteer, now: number): string {
  const when = startedLabel(steer.at, now);
  if (steer.kind === "amendment") return `Amendment · ${when} · by ${steer.author}`;
  const run = steer.run ?? steer.read_by;
  return run == null ? `Steer · ${when}` : `Steer · run ${run} · ${when}`;
}

function fateOf(steer: TicketSteer): { text: string; read: boolean } {
  if (steer.withdrawn) return { text: "withdrawn", read: false };
  if (steer.read_ms != null) return { text: `the agent read it at ${formatClock(steer.read_ms)}`, read: true };
  if (steer.kind === "hint") return { text: "waiting for the agent to read it", read: false };
  return { text: "every run since reads it with the ticket", read: false };
}

/** The ticket's steer notes and amendments, newest first, each with what became of it. */
export function SteerNotes({ steers, now }: { steers: TicketSteer[]; now: number }) {
  const newest = [...steers].sort((a, b) => b.at - a.at || b.id - a.id);
  return (
    <section aria-labelledby="ticket-steers" className="mt-[26px]">
      <div id="ticket-steers" className={SECTION_LABEL}>
        <span>
          Steer notes and amendments <span className="text-ink">{steers.length}</span>
        </span>
      </div>
      {newest.length > 0 && (
        <ul className="m-0 mt-2 list-none p-0">
          {newest.map((steer) => {
            const fate = fateOf(steer);
            return (
              <li key={steer.id} data-steer={steer.id} className="border-b border-dashed border-rule-2 py-3 last:border-b-0">
                <div data-steer-head className="font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-2">
                  {headOf(steer, now)}
                </div>
                <p className="m-0 mt-2 max-w-[75ch] whitespace-pre-wrap font-read text-[15px] leading-[1.5] text-ink">{steer.note}</p>
                <div data-steer-fate className={`mt-1.5 font-mono text-[12px] leading-[1.4] ${fate.read ? "text-live" : "text-ink-3"}`}>
                  {fate.text}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
