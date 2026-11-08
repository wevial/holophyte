import { useId, useState, type Ref } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { SCOUT_DOTS } from "../scout";
import type { ScoutState } from "../scouts";
import { FoldChevron } from "./icons";
import { scoutMeta } from "./ScoutFacts";
import { StateDot } from "./StateDot";

const TITLE = "m-0 max-w-[56ch] font-sans text-[26px] font-semibold leading-[1.2] tracking-[-.005em] text-balance text-ink [overflow-wrap:anywhere]";

/** The full ask behind a closed Ask disclosure. */
function AskFold({ ask }: { ask: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="mt-2">
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        className="group inline-flex cursor-pointer items-center gap-1 border-0 bg-transparent p-0 font-mono text-[12px] leading-[1.7] text-ink-3 hover:text-ink">
        <FoldChevron open={open} />
        Ask
      </button>
      {open && <p id={id} data-scout-ask className="m-0 mt-2 max-w-[70ch] whitespace-pre-wrap font-read text-[15px] leading-[1.55] text-ink-2 [overflow-wrap:anywhere]">{ask}</p>}
    </div>
  );
}

/** A scout's header: its state dot and title, then its meta line and its Ask. */
export function ScoutHead({ detail, project, state, now, headingRef }: {
  detail: ScoutDetail;
  project: string;
  state: ScoutState;
  now: number;
  headingRef?: Ref<HTMLHeadingElement>;
}) {
  const { scout } = detail;
  return (
    <div className="grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5">
      <span className="mt-[6px]"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
      <h1 ref={headingRef} tabIndex={-1} className={TITLE}>{scout.title}</h1>
      <div className="col-span-2 mt-2">
        <p data-scout-meta className="m-0 font-mono text-[12.5px] leading-[1.6] text-ink-3 [overflow-wrap:anywhere]">{scoutMeta(detail, now)}</p>
        <AskFold key={`${project}/${scout.id}`} ask={scout.ask} />
      </div>
    </div>
  );
}
