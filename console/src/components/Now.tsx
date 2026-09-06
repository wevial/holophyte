import { useState } from "react";
import type { ProjectChoice } from "../lib/attention";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { Attention, Status } from "../lib/types";
import { Floor } from "./Floor";
import { NeedsYou } from "./NeedsYou";
import { RunDetail } from "./RunDetail";

/** The Now view: the needs-you band over the Floor. The one expanded run
 *  lives here and its detail card reads `/runs/N` from `base`, refreshed
 *  each time `polls` advances. */
export function Now({
  attention,
  status,
  project,
  base,
  polls = 0,
  deps = defaultPollDeps,
}: {
  attention: Attention;
  status: Status;
  project: ProjectChoice;
  base: string;
  polls?: number;
  deps?: { fetch: Fetch };
}) {
  const [expandedRun, setExpandedRun] = useState<number | null>(null);
  const toggleRun = (id: number) => setExpandedRun((previous) => (previous === id ? null : id));
  return (
    <>
      <NeedsYou attention={attention} status={status} project={project} />
      <Floor
        statuses={[status]}
        project={project}
        expandedRun={expandedRun}
        onToggleRun={toggleRun}
        renderDetail={(run) => <RunDetail base={base} id={run.id} now={status.now} polls={polls} deps={deps} />}
      />
    </>
  );
}
