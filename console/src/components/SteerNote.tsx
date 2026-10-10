import { useState } from "react";
import type { RowDaemon } from "./RowActions";
import { ReasonAction } from "./ReasonAction";

/** A maintainer's note steered into `ticket`'s work. `stoppable` offers
 *  "Stop the turn now", which only a live run honours. */
export function SteerNote({ daemon, ticket, stoppable = false }: { daemon: RowDaemon; ticket: string; stoppable?: boolean }) {
  const [hint, setHint] = useState(false);
  const [now, setNow] = useState(false);
  return <ReasonAction daemon={daemon} route="/actions/steer" body={{ ticket, hint, now: stoppable && now }}
    label="Steer" boxLabel="Steer note" onClose={() => { setHint(false); setNow(false); }}>
    <label className="flex items-center gap-1 text-[12px]">
      <input type="checkbox" checked={hint} onChange={(event) => setHint(event.target.checked)} />Hint only
    </label>
    {stoppable && <label className="flex items-center gap-1 text-[12px]">
      <input type="checkbox" checked={now} onChange={(event) => setNow(event.target.checked)} />Stop the turn now
    </label>}
  </ReasonAction>;
}
