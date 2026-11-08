import type { Ref } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { SCOUT_DOTS } from "../scout";
import type { ScoutState } from "../scouts";
import { scoutMeta } from "./ScoutFacts";
import { StateDot } from "./StateDot";

const TITLE = "m-0 max-w-[56ch] font-sans text-[26px] font-semibold leading-[1.2] tracking-[-.005em] text-balance text-ink [overflow-wrap:anywhere]";

/** A scout's header: its state dot and title, then its meta line. */
export function ScoutHead({ detail, state, now, headingRef }: {
  detail: ScoutDetail;
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
      </div>
    </div>
  );
}
