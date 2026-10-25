import { useId, useState, type ReactNode } from "react";
import type { CommandEvidence, FileEvidence, Hypothesis, Reproduction, ScoutFinding, ScoutReportPass, TextEvidence } from "../../lib/contract.gen";
import { shortSha } from "../../lib/shipped";
import { numberFindings, type NumberedFinding } from "../scout";
import { SECTION_LABEL } from "./Criteria";
import { FoldChevron } from "./icons";

type Evidence = ScoutFinding["evidence"][number];

const CHIP = "inline-block rounded px-2 py-0.5 font-sans text-[13px] font-medium leading-[1.4]";
const MONO = "font-mono text-[13px] leading-[1.5] [overflow-wrap:anywhere]";
const ID = "pt-[5px] font-mono text-[11px] font-semibold leading-none text-ink-3";
const ROW = "grid grid-cols-[2.375rem_6.75rem_minmax(0,1fr)] gap-x-2 border-b border-rule-2 py-4 max-[720px]:grid-cols-[2.375rem_minmax(0,1fr)]";
const TEXT = "m-0 max-w-[70ch] font-sans text-[15px] leading-[1.45] text-ink [overflow-wrap:anywhere]";
const TOGGLE = "group inline-flex cursor-pointer items-center gap-1 border-0 bg-transparent p-0 font-mono text-[12px] leading-[1.5] text-ink-3 hover:text-ink";

const FINDING_TONES: Record<ScoutFinding["status"], string> = {
  confirmed: "bg-live-wash text-ok-ink",
  partial: "bg-wait-wash text-wait",
  unconfirmed: "bg-rule-2 text-ink-2",
};

const VERDICTS: Record<Hypothesis["verdict"], { word: string; tone: string }> = {
  supported: { word: "supported", tone: "bg-live-wash text-ok-ink" },
  ruled_out: { word: "ruled out", tone: "bg-rule-2 text-ink-2" },
  open: { word: "open", tone: "bg-wait-wash text-wait" },
};

const LEVELS: Record<Reproduction["level"], string> = {
  test: "reproduced in a test",
  real_tool: "reproduced with the real tool",
  not_reproduced: "not reproduced",
};

const isFile = (item: Evidence): item is FileEvidence => "file" in item;
const isCommand = (item: Evidence): item is CommandEvidence => "command" in item;
const isText = (item: Evidence): item is TextEvidence => "text" in item;

function FileItem({ item, base }: { item: FileEvidence; base: string }) {
  const place = `${item.file}:${item.line}`;
  if (item.verified) {
    return <span data-file title={base === "" ? undefined : `at base ${base}`} className={`${MONO} text-ink underline underline-offset-2`}>{place}</span>;
  }
  return (
    <span data-file data-unverified title={item.why ?? undefined} className={`${MONO} text-warn-ink`}>
      ? {place} unverified
    </span>
  );
}

function UrlItem({ url }: { url: string }) {
  if (!/^https?:\/\//i.test(url)) return <span data-url className={`${MONO} text-ink-2`}>{url}</span>;
  return (
    <a data-url href={url} target="_blank" rel="noreferrer" className={`${MONO} text-ink-2 underline underline-offset-2 hover:text-ink`}>
      {url.replace(/^https?:\/\//i, "")}
    </a>
  );
}

function EvidenceItems({ evidence, base }: { evidence: Evidence[]; base: string }) {
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const outputId = useId();
  const toggle = (index: number) => {
    const next = new Set(open);
    if (!next.delete(index)) next.add(index);
    setOpen(next);
  };
  return (
    <ul data-evidence className="m-0 mt-2 flex list-none flex-col items-start gap-1 p-0">
      {evidence.map((item, index) => {
        let shown: ReactNode;
        if (isFile(item)) shown = <FileItem item={item} base={base} />;
        else if (isCommand(item)) {
          shown = (
            <>
              <button
                type="button"
                aria-expanded={open.has(index)}
                aria-controls={`${outputId}-${index}`}
                onClick={() => toggle(index)}
                className={`${MONO} cursor-pointer rounded border-0 bg-act-wash px-2 py-px text-left text-act hover:underline`}
              >
                {item.command} {open.has(index) ? "▾" : "▸"}
              </button>
              {open.has(index) && (
                <pre id={`${outputId}-${index}`} data-command-output className="m-0 mt-2 max-h-80 overflow-auto whitespace-pre-wrap border border-rule-2 bg-paper px-3 py-2 font-mono text-[12px] leading-[1.5] text-ink-2 [overflow-wrap:anywhere]">
                  <span className="text-ink">$ {item.command}</span>
                  {"\n"}
                  {item.output}
                </pre>
              )}
            </>
          );
        } else if (isText(item)) shown = <span data-text className={`${MONO} text-ink-2`}>{item.text}</span>;
        else shown = <UrlItem url={item.url} />;
        return <li key={index} className="max-w-full">{shown}</li>;
      })}
    </ul>
  );
}

const citations = (n: number) => `${n} ${n === 1 ? "citation" : "citations"}`;

function FindingItem({ numbered, base }: { numbered: NumberedFinding; base: string }) {
  const { id, pass, finding } = numbered;
  const [open, setOpen] = useState(false);
  const evidenceId = useId();
  return (
    <li data-scout-finding className={`${ROW} px-4 last:border-b-0`}>
      <span data-finding-id className={ID}>{id}</span>
      <span><span data-finding-status className={`${CHIP} whitespace-nowrap ${FINDING_TONES[finding.status]}`}>{finding.status}</span></span>
      <div className="min-w-0 max-[720px]:col-start-2">
        <p className={TEXT}>
          <span data-finding-title>{finding.title}</span>
          {pass > 1 && <span data-finding-pass className="ml-2 rounded border border-rule px-1.5 py-px font-mono text-[11px] text-ink-3">pass {pass}</span>}
        </p>
        {finding.evidence.length > 0 && (
          <>
            <button type="button" aria-expanded={open} aria-controls={evidenceId} onClick={() => setOpen(!open)} className={`mt-2 ${TOGGLE}`}>
              <FoldChevron open={open} />
              {citations(finding.evidence.length)}
            </button>
            {open && <div id={evidenceId}><EvidenceItems evidence={finding.evidence} base={base} /></div>}
          </>
        )}
      </div>
    </li>
  );
}

function Hypotheses({ hypotheses }: { hypotheses: Hypothesis[] }) {
  return (
    <>
      <ol aria-label="Hypotheses" className="m-0 mt-2 list-none p-0">
        {hypotheses.map((each, index) => {
          const verdict = VERDICTS[each.verdict];
          return (
            <li key={index} data-hypothesis className={ROW}>
              <span data-hypothesis-id className={ID}>H{index + 1}</span>
              <span><span data-verdict className={`${CHIP} ${verdict.tone}`}>{verdict.word}</span></span>
              <div className="min-w-0 max-[720px]:col-start-2">
                <p data-hypothesis-text className={TEXT}>{each.hypothesis}</p>
                <p data-separates className="m-0 mt-0.5 font-sans text-[14px] leading-[1.45] text-ink-2 [overflow-wrap:anywhere]">{each.separates}</p>
              </div>
            </li>
          );
        })}
      </ol>
    </>
  );
}

const VERDICT_ORDER: Hypothesis["verdict"][] = ["supported", "ruled_out", "open"];

/** "not reproduced · 5 hypotheses · 2 ruled out · 3 open", leaving out a count of 0; with hypotheses it opens their
 *  list, without it reads its level alone. */
function ReproductionLine({ reproduction }: { reproduction: Reproduction }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const { hypotheses } = reproduction;
  const words = [
    LEVELS[reproduction.level],
    ...(hypotheses.length ? [`${hypotheses.length} ${hypotheses.length === 1 ? "hypothesis" : "hypotheses"}`] : []),
    ...VERDICT_ORDER.flatMap((verdict) => {
      const n = hypotheses.filter((each) => each.verdict === verdict).length;
      return n ? [`${n} ${VERDICTS[verdict].word}`] : [];
    }),
  ].join(" · ");
  if (hypotheses.length === 0) return <p data-reproduction className="m-0 mt-4 font-mono text-[12px] leading-[1.5] text-ink-3">{words}</p>;
  return (
    <div className="mt-4">
      <button type="button" data-reproduction aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)} className={TOGGLE}>
        <FoldChevron open={open} />
        {words}
      </button>
      {open && <div id={id}><Hypotheses hypotheses={hypotheses} /></div>}
    </div>
  );
}

/** A done scout's Findings: every pass's findings numbered F1… with their citations folded, then the newest report's
 *  reproduction line. */
export function ScoutReport({ reports, baseSha }: { reports: ScoutReportPass[]; baseSha: string | null }) {
  if (reports.length === 0) return null;
  const passes = [...reports].sort((a, b) => a.pass - b.pass);
  const newest = passes.at(-1)!.report;
  const findings = numberFindings(passes);
  const base = shortSha(baseSha);
  return (
    <section aria-label="Findings" className="mt-10">
      <h2 className={`m-0 ${SECTION_LABEL}`}>
        Findings
        <span data-report-count className="text-ink">{findings.length}</span>
      </h2>
      {findings.length > 0 && (
        <ol aria-label="Findings" className="m-0 mt-2 list-none border border-rule bg-findings-bg p-0">
          {findings.map((each) => <FindingItem key={each.id} numbered={each} base={base} />)}
        </ol>
      )}
      {newest.reproduction != null && <ReproductionLine reproduction={newest.reproduction} />}
    </section>
  );
}
