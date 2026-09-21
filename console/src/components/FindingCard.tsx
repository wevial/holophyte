import { useState } from "react";
import { FATE_LABEL, findingParts, findingPath, severityOf, type Fate } from "../lib/findings";
import { Markdown } from "./Markdown";
import type { Finding } from "../lib/types";
import { SeverityPill } from "./SeverityPill";

const FATE_TONES: Record<Fate, string> = {
  fixed: "bg-ok-bg text-ok-text",
  declined: "border border-chip-border text-muted",
  follow_up: "bg-warn-bg text-warn-text",
  open: "bg-bad-bg text-bad-text",
};

/** One open finding: severity pill, `path:line`, then the reviewer's
 *  message read as a bold-lead title and a Markdown body. A criterion
 *  finding titles "Criterion n · status" and folds the criterion's own
 *  text under a disclosure. On a finished run's history the card also
 *  carries `fate` — a chip after the pill — and `sentence`, the
 *  implementer's adjudication line, under the body. */
export function FindingCard({
  finding,
  fate,
  sentence,
}: {
  finding: Finding;
  fate?: Fate;
  sentence?: string | null;
}) {
  const [originalOpen, setOriginalOpen] = useState(false);
  const path = findingPath(finding);
  const location = finding.line != null ? `${path}:${finding.line}` : path;
  const parts = finding.summary != null
    ? { title: null, body: finding.summary, criterion: null }
    : findingParts(finding.message);
  return (
    <li
      data-finding
      className="rounded-[10px] border border-line bg-card px-4 py-[14px] shadow-card"
    >
      <div className="flex items-center gap-2">
        <SeverityPill severity={severityOf(finding)} />
        {finding.verdict && <span data-verdict className="rounded-pill bg-warn-bg px-2 py-[3px] font-mono text-[11px] text-warn-text">{finding.verdict}</span>}
        {finding.author && <span data-author className="text-[12px] text-muted">@{finding.author}</span>}
        {fate != null && (
          <span
            data-fate={fate}
            className={`inline-block shrink-0 rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${FATE_TONES[fate]}`}
          >
            {FATE_LABEL[fate]}
          </span>
        )}
        <span data-location className="truncate font-mono text-[12px] text-link">
          {finding.url ? <a href={finding.url} target="_blank" rel="noreferrer">{location}</a> : location}
        </span>
      </div>
      {parts.title != null && (
        <p data-title className="mt-2 text-[14px] font-semibold leading-[1.45] text-ink">
          {parts.title}
        </p>
      )}
      {parts.body !== "" && (
        <div data-body className="mt-1 text-[14px] leading-[1.45] text-body">
          <Markdown>{parts.body}</Markdown>
        </div>
      )}
      {sentence != null && (
        <p data-fate-sentence className="mt-1 text-[12px] leading-[1.45] text-muted">
          {sentence}
        </p>
      )}
      {parts.criterion != null && <CriterionFold text={parts.criterion} />}
      {finding.raw != null && (
        <div className="mt-2">
          <button type="button" aria-expanded={originalOpen}
            className="font-mono text-[11px] text-muted"
            onClick={() => setOriginalOpen((open) => !open)}>
            {originalOpen ? "Hide original comment" : "Show original comment"}
          </button>
          {originalOpen && <div data-original className="mt-2 text-[13px] text-muted"><Markdown>{finding.raw}</Markdown></div>}
        </div>
      )}
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
      <div data-criterion hidden={!open} className="mt-1 text-[13px] leading-[1.45] text-muted">
        <Markdown>{text}</Markdown>
      </div>
    </div>
  );
}
