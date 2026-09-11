import { useState } from "react";
import { findingParts, findingPath, severityOf } from "../lib/findings";
import { renderMarkdown } from "../lib/markdown";
import type { Finding } from "../lib/types";
import { SeverityPill } from "./SeverityPill";

/** One open finding: severity pill, `path:line`, then the reviewer's
 *  message read as a bold-lead title and a Markdown body. A criterion
 *  finding titles "Criterion n · status" and folds the criterion's own
 *  text under a disclosure. */
export function FindingCard({ finding }: { finding: Finding }) {
  const path = findingPath(finding);
  const location = finding.line != null ? `${path}:${finding.line}` : path;
  const parts = findingParts(finding.message);
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
      {parts.title != null && (
        <p data-title className="mt-2 text-[14px] font-semibold leading-[1.45] text-ink">
          {parts.title}
        </p>
      )}
      {parts.body !== "" && (
        <div data-body className="ticket-body mt-1 text-[14px] leading-[1.45] text-body">
          {renderMarkdown(parts.body)}
        </div>
      )}
      {parts.criterion != null && <CriterionFold text={parts.criterion} />}
    </li>
  );
}

/** The criterion's own text behind a fold labelled "criterion", closed
 *  until clicked — the reason is the body; this is the contract it failed. */
function CriterionFold({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((previous) => !previous)}
        className="flex items-baseline gap-1 font-mono text-[11px] text-muted"
      >
        <span data-chevron={open ? "open" : "closed"} aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        criterion
      </button>
      <div data-criterion hidden={!open} className="ticket-body mt-1 text-[13px] leading-[1.45] text-muted">
        {renderMarkdown(text)}
      </div>
    </div>
  );
}
