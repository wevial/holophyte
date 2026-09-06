import { severityOf } from "../lib/findings";
import type { Finding } from "../lib/types";
import { SeverityPill } from "./SeverityPill";

/** One open finding: severity pill, `path:line`, the reviewer's message. */
export function FindingCard({ finding }: { finding: Finding }) {
  const location = finding.line != null ? `${finding.path}:${finding.line}` : finding.path;
  return (
    <li
      data-finding
      className="rounded-[10px] border border-line bg-card px-4 py-[14px] shadow-card"
    >
      <div className="flex items-center gap-2">
        <SeverityPill severity={severityOf(finding)} />
        <span data-location className="truncate font-mono text-[12px] text-link">
          {location}
        </span>
      </div>
      <p className="mt-2 text-[14px] leading-[1.45] text-body">{finding.message}</p>
    </li>
  );
}
