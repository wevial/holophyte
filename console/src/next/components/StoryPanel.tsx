import { Link } from "@tanstack/react-router";
import type { BoardStory, StoryChildRow, StoryDetail } from "../../lib/contract.gen";
import { stateOf, type State } from "../state";
import { SECTION_LABEL } from "./Criteria";
import { StateDot } from "./StateDot";

const SUB = "m-0 font-sans text-[12px] leading-[1.4] text-ink-3";
const KEY = "mr-1 font-mono text-[11px] font-medium leading-[1.4] tabular-nums text-ink-3";

function childState(child: StoryChildRow): State {
  if (child.status === "merged") return "merged";
  if (child.status === "abandoned") return "backlog";
  return stateOf({ board: child.column === "backlog" ? "backlog" : child.status });
}

function ChildRow({ child, project, current }: { child: StoryChildRow; project: string; current: boolean }) {
  const word = child.column === "backlog" ? "backlog" : child.status.replaceAll("_", " ");
  return (
    <li data-child={child.ticket} className="flex items-start gap-2 border-b border-dashed border-rule-2 py-[7px] text-ink">
      <span className="mt-0.5 inline-flex">
        <StateDot state={childState(child)} hideWord />
      </span>
      <span className="min-w-0">
        {current ? (
          <span className={KEY}>{child.ticket}</span>
        ) : (
          <Link to="/tickets/$project/$key" params={{ project, key: child.ticket }} className={`${KEY} no-underline hover:underline`}>
            {child.ticket}
          </Link>
        )}{" "}
        {child.title}{" "}
        <span className="ml-1 font-mono text-[11.5px] font-normal leading-[1.4] text-ink-3">
          {word}
          {current && " · this ticket"}
        </span>
      </span>
    </li>
  );
}

/** The story `/board` places the ticket in: its parent and progress for a
 *  child, then the story's tickets once `/stories/KEY` answers. */
export function StoryPanel({ ticketKey, project, entry, detail }: { ticketKey: string; project: string; entry: BoardStory; detail: StoryDetail | null }) {
  const own = entry.ticket === ticketKey;
  return (
    <section aria-labelledby="ticket-story" className="mt-[26px]">
      <div id="ticket-story" className={SECTION_LABEL}>Story</div>
      {own ? (
        detail != null && <p className={`${SUB} mt-2.5`}>Child tickets</p>
      ) : (
        <>
          <p className={`${SUB} mt-2.5`}>Parent</p>
          <Link
            to="/tickets/$project/$key"
            params={{ project, key: entry.ticket }}
            className="mt-1 inline-block font-sans text-[14px] font-semibold leading-[1.35] text-ink no-underline hover:underline"
          >
            <span className={KEY}>{entry.ticket}</span> {entry.title}
          </Link>
          <p className="m-0 mt-1.5 font-mono text-[12px] leading-[1.4] text-ink-3">
            {entry.merged} of {entry.total} merged
          </p>
        </>
      )}
      {detail != null &&
        (detail.children.length === 0 ? (
          <p className="m-0 mt-3.5 font-mono text-[12px] leading-[1.4] text-ink-3">No child tickets.</p>
        ) : (
          <ul aria-label={`${entry.ticket} tickets`} className="m-0 mt-3.5 list-none p-0 font-sans text-[13.5px] font-semibold leading-[1.4]">
            {detail.children.map((child) => (
              <ChildRow key={child.ticket} child={child} project={project} current={child.ticket === ticketKey} />
            ))}
          </ul>
        ))}
    </section>
  );
}
