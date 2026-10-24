import { useId, useState } from "react";
import type { CommandEvidence, FileEvidence, Hypothesis, Reproduction, ScoutFinding, ScoutReportPass } from "../../lib/contract.gen";
import { shortSha } from "../../lib/shipped";
import { numberFindings, type NumberedFinding } from "../scout";
import { SECTION_LABEL } from "./Criteria";

type Evidence = ScoutFinding["evidence"][number];

const CHIP = "inline-block rounded px-2 py-0.5 font-sans text-[13px] font-medium leading-[1.4]";
const MONO = "font-mono text-[13px] leading-[1.5] [overflow-wrap:anywhere]";
const ID = "pt-[5px] font-mono text-[11px] font-semibold leading-none text-ink-3";
const ROW = "grid grid-cols-[2.375rem_5.25rem_minmax(0,1fr)] gap-x-2 border-b border-rule-2 py-3 max-[720px]:grid-cols-[2.375rem_minmax(0,1fr)]";
const TEXT = "m-0 font-sans text-[15px] leading-[1.45] text-ink [overflow-wrap:anywhere]";

const FINDING_TONES: Record<ScoutFinding["status"], string> = {
  shown: "bg-live-wash text-ok-ink",
  likely: "bg-wait-wash text-wait",
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

function FindingItem({ numbered, base }: { numbered: NumberedFinding; base: string }) {
  const { id, pass, finding } = numbered;
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const outputId = useId();
  const toggle = (index: number) => {
    const next = new Set(open);
    if (!next.delete(index)) next.add(index);
    setOpen(next);
  };
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
          <div data-evidence className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {finding.evidence.map((item, index) => {
              if (isFile(item)) return <FileItem key={index} item={item} base={base} />;
              if (isCommand(item)) {
                const shown = open.has(index);
                return (
                  <button
                    key={index}
                    type="button"
                    aria-expanded={shown}
                    aria-controls={`${outputId}-${index}`}
                    onClick={() => toggle(index)}
                    className={`${MONO} cursor-pointer rounded border-0 bg-act-wash px-2 py-px text-left text-act hover:underline`}
                  >
                    {item.command} {shown ? "▾" : "▸"}
                  </button>
                );
              }
              return <UrlItem key={index} url={item.url} />;
            })}
          </div>
        )}
        {finding.evidence.map((item, index) =>
          isCommand(item) && open.has(index) ? (
            <pre key={index} id={`${outputId}-${index}`} data-command-output className="m-0 mt-2 max-h-80 overflow-auto whitespace-pre-wrap border border-rule-2 bg-paper px-3 py-2 font-mono text-[12px] leading-[1.5] text-ink-2 [overflow-wrap:anywhere]">
              <span className="text-ink">$ {item.command}</span>
              {"\n"}
              {item.output}
            </pre>
          ) : null,
        )}
      </div>
    </li>
  );
}

function Hypotheses({ hypotheses }: { hypotheses: Hypothesis[] }) {
  return (
    <>
      <h3 className="m-0 mt-5 font-sans text-[13px] font-semibold leading-none text-ink-2">Hypotheses</h3>
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

/** A done scout's report: every pass's findings numbered F1… with their evidence, and the newest pass's hypotheses. */
export function ScoutReport({ reports, baseSha }: { reports: ScoutReportPass[]; baseSha: string | null }) {
  if (reports.length === 0) return null;
  const passes = [...reports].sort((a, b) => a.pass - b.pass);
  const newest = passes.at(-1)!.report;
  const findings = numberFindings(passes);
  const base = shortSha(baseSha);
  const meta = [
    newest.reproduction == null ? null : LEVELS[newest.reproduction.level],
    base === "" ? null : `at base ${base}`,
    passes.length > 1 ? `${passes.length} passes` : null,
  ].filter((part) => part != null);
  const hypotheses = newest.reproduction?.hypotheses ?? [];
  return (
    <section aria-label="Report" className="mt-6">
      <h2 className={`m-0 ${SECTION_LABEL}`}>
        Report
        <span data-report-count className="text-ink">{findings.length} {findings.length === 1 ? "finding" : "findings"}</span>
      </h2>
      {meta.length > 0 && <p data-report-meta className="m-0 mt-2.5 font-mono text-[13px] leading-[1.5] text-ink-2">{meta.join(" · ")}</p>}
      {findings.length > 0 && (
        <ol aria-label="Findings" className="m-0 mt-3 list-none border border-rule bg-paper-2 p-0">
          {findings.map((each) => <FindingItem key={each.id} numbered={each} base={base} />)}
        </ol>
      )}
      {hypotheses.length > 0 && <Hypotheses hypotheses={hypotheses} />}
    </section>
  );
}
