import { useId, useState, type ReactNode, type Ref } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { SCOUT_DOTS } from "../scout";
import type { ScoutState } from "../scouts";
import { FoldChevron } from "./icons";
import { ScoutCost, ScoutWhat } from "./ScoutFacts";
import { StateDot } from "./StateDot";

const TITLE = "m-0 max-w-[56ch] font-sans text-[20px] font-semibold leading-[1.3] tracking-[-.005em] text-ink [overflow-wrap:anywhere]";
const FACTS = "m-0 font-mono text-[12px] leading-[1.7] text-ink-3 [overflow-wrap:anywhere]";

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

/** A scout's header: its state dot and title (`h1` on its page, `h2` in the pane) with `buttons` at its right, then
 *  its what and cost lines and its Ask. */
export function ScoutHead({ detail, project, state, now, pass, level, headingRef, buttons }: {
  detail: ScoutDetail;
  project: string;
  state: ScoutState;
  now: number;
  pass: { pass: number; ms: number | null } | null;
  level: 1 | 2;
  headingRef?: Ref<HTMLHeadingElement>;
  buttons: ReactNode;
}) {
  const { scout } = detail;
  const Heading = level === 1 ? "h1" : "h2";
  return (
    <div className="grid grid-cols-[16px_minmax(0,1fr)_auto] gap-x-2 max-[720px]:grid-cols-[16px_minmax(0,1fr)]">
      <span className="mt-[5px]"><StateDot state={SCOUT_DOTS[state]} label={state} hideWord /></span>
      <Heading ref={headingRef} tabIndex={-1} className={TITLE}>{scout.title}</Heading>
      <div className="row-span-2 flex shrink-0 flex-wrap items-start justify-end gap-2 max-[720px]:col-start-2 max-[720px]:row-span-1 max-[720px]:mt-2 max-[720px]:justify-start">{buttons}</div>
      <div className="col-start-2 mt-2">
        <div data-scout-facts>
          <p data-scout-what className={FACTS}><ScoutWhat detail={detail} project={project} /></p>
          {scout.phase !== "queued" && scout.started_ms != null && (
            <p data-scout-cost className={FACTS}><ScoutCost detail={detail} now={now} pass={pass} /></p>
          )}
        </div>
        <AskFold key={`${project}/${scout.id}`} ask={scout.ask} />
      </div>
    </div>
  );
}
