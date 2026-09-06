import { useState } from "react";
import type { ProjectChoice } from "../lib/attention";
import type { Attention, Status } from "../lib/types";
import { Floor } from "./Floor";
import { NeedsYou } from "./NeedsYou";

/** The Now view: the needs-you band over the Floor. The one expanded run
 *  lives here so the detail ticket can render into its slot. */
export function Now({ attention, status, project }: { attention: Attention; status: Status; project: ProjectChoice }) {
  const [expandedRun, setExpandedRun] = useState<number | null>(null);
  const toggleRun = (id: number) => setExpandedRun((previous) => (previous === id ? null : id));
  return (
    <>
      <NeedsYou attention={attention} status={status} project={project} />
      <Floor statuses={[status]} project={project} expandedRun={expandedRun} onToggleRun={toggleRun} />
    </>
  );
}
