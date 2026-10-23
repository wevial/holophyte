import { useId, useState } from "react";
import type { RoundFinding } from "../../lib/contract.gen";
import { findingParts, findingPath, severityOf, type Fate } from "../../lib/findings";
import { shortSha } from "../../lib/shipped";
import { SeverityPill } from "./FindingRow";
import { TitleText } from "./TitleText";

interface Outcome {
  word: string;
  tone: string;
  detail: string | null;
}

const CHIP = "rounded px-2 py-0.5 font-sans text-[13px] font-medium leading-[1.4]";
const ADDRESSED_IN = /^\s*Addressed in ([0-9a-f]{7,40})\b/i;
const CRITERION_TITLE = /^Criterion \d+ · /;
const LEADING_PUNCTUATION = /^[\s.,;:–—-]+/;

const plain = (text: string) => text.replaceAll("**", "").trim();

function threadOutcome(finding: RoundFinding): Outcome | null {
  const verdict = (finding.verdict ?? "").toUpperCase().replace(/[\s-]/g, "_");
  const reply = finding.reply ?? "";
  if (verdict === "ADDRESS") return { word: "Addressed", tone: "bg-live-wash text-ok-ink", detail: shortSha(ADDRESSED_IN.exec(reply)?.[1] ?? null) || null };
  if (verdict === "DECLINE") {
    const reason = reply.replace(/^\s*Declined\s*:\s*/i, "").split("\n", 1)[0]!.trim();
    return { word: "Declined", tone: "bg-rule-2 text-ink", detail: reason || null };
  }
  if (verdict === "FOLLOW_UP") return { word: "Follow-up", tone: "bg-wait-wash text-wait", detail: null };
  return null;
}

/** The title a finding reads by, and its text beyond that title. */
function readFinding(finding: RoundFinding): { title: string; more: string } {
  const parts = findingParts(finding.message);
  if (parts.title != null) {
    const more = [parts.body.replace(LEADING_PUNCTUATION, ""), parts.criterion ?? ""].filter(Boolean).join("\n");
    return { title: plain(parts.title), more: plain(more) };
  }
  if (finding.summary) {
    const message = plain(parts.body);
    return { title: plain(finding.summary), more: message === plain(finding.summary) ? "" : message };
  }
  const [first = "", ...rest] = parts.body.split("\n");
  return { title: plain(first), more: plain(rest.join("\n")) };
}

const criterionId = (criterion: string) => (/^\d+$/.test(criterion) ? `C${criterion}` : criterion);

function Location({ finding }: { finding: RoundFinding }) {
  const path = findingPath(finding);
  if (path === "") return null;
  const text = finding.line == null ? path : `${path}:${finding.line}`;
  const className = "min-w-0 font-mono text-[13px] text-ink [overflow-wrap:anywhere]";
  if (finding.url == null) return <span data-location className={className}>{text}</span>;
  return (
    <a data-location href={finding.url} target="_blank" rel="noreferrer" className={`${className} underline underline-offset-2 hover:text-ink-2`}>
      {text}
    </a>
  );
}

/** One finding as two lines: its severity and title, then its outcome, detail and place; More opens its whole text. */
export function ActivityFinding({ finding, fate }: { finding: RoundFinding; fate: Fate | null }) {
  const [open, setOpen] = useState(false);
  const moreId = useId();
  const { title, more } = readFinding(finding);
  const thread = finding.kind === "thread" ? threadOutcome(finding) : null;
  const outcome = thread ?? (fate === "fixed" ? { word: "not raised again", tone: "bg-rule-2 text-ink-2", detail: null } : null);
  const criterion = finding.criterion ? criterionId(finding.criterion) : null;
  const evidence = finding.evidence == null ? null : [finding.evidence, finding.reviewer].filter(Boolean).join(" · ");
  return (
    <li data-activity-finding className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 border-b border-rule-2 px-4 py-3 last:border-b-0">
      <span className="pt-[3px]"><SeverityPill severity={severityOf(finding)} /></span>
      <div className="min-w-0">
        <div className="flex items-baseline gap-x-3">
          <span data-finding-title className={`min-w-0 flex-1 font-sans text-[15px] leading-[1.45] text-ink [overflow-wrap:anywhere] ${open ? "" : "line-clamp-2"}`}>
            <TitleText>{title}</TitleText>
          </span>
          {more !== "" && (
            <button
              type="button"
              aria-expanded={open}
              aria-controls={moreId}
              onClick={() => setOpen(!open)}
              className="shrink-0 cursor-pointer border-0 bg-transparent p-0 font-mono text-[12px] text-ink-2 underline underline-offset-2 hover:text-ink"
            >
              {open ? "Less" : "More"}
            </button>
          )}
        </div>
        {more !== "" && (
          <p id={moreId} hidden={!open} data-finding-more className="m-0 mt-1 whitespace-pre-line font-sans text-[14px] leading-[1.5] text-ink-2 [overflow-wrap:anywhere]">
            {open && more}
          </p>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          {outcome != null && <span data-outcome className={`${CHIP} ${outcome.tone}`}>{outcome.word}</span>}
          {outcome?.detail != null && <span data-outcome-detail className={`font-sans text-[14px] text-ink-2 ${thread?.word === "Addressed" ? "font-mono" : ""}`}>{outcome.detail}</span>}
          {evidence != null && <span data-evidence className={`${CHIP} bg-act-wash text-act`}>{evidence}</span>}
          {criterion != null && <span data-also className="font-sans text-[14px] text-ink-2">also {criterion}</span>}
          {!CRITERION_TITLE.test(title) && <Location finding={finding} />}
        </div>
      </div>
    </li>
  );
}
