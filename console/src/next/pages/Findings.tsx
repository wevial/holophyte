import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { CleanReviewLine, FindingRow } from "../components/FindingRow";
import { StateDot } from "../components/StateDot";
import { useFindingsPage, type FindingRowView, type FindingsPage, type Idle, type OpenFinding } from "../findings";
import { storeScope } from "../projects";
import type { State } from "../state";

const WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"];

const word = (count: number) => WORDS[count] ?? String(count);
const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const plural = (count: number, one: string, many = `${one}s`) => (count === 1 ? one : many);

const IDLE: Record<Idle, string> = {
  stale: "is stale",
  failed: "has failed",
  paused: "is paused",
  not_running: "is not running",
};

function openSentence(open: OpenFinding[]): string {
  const idle = open.filter((each) => each.idle != null);
  const fixing = open.length - idle.length;
  if (open.length === 1) {
    const only = idle[0];
    return only == null ? "One finding is open and already being fixed." : `One finding is open and nobody is fixing it: ${only.ticket} ${IDLE[only.idle!]}.`;
  }
  const head = `${capital(word(open.length))} findings are still open, and`;
  if (fixing === 0) return `${head} nobody is fixing them.`;
  if (idle.length === 0) return `${head} all are being fixed.`;
  return `${head} ${word(fixing)} ${plural(fixing, "is", "are")} already being fixed.`;
}

function settledSentence(page: FindingsPage): string {
  if (page.reviews === 0) return "No reviews this week.";
  const reviews = `${capital(word(page.reviews))} ${plural(page.reviews, "review")} this week`;
  if (page.findings === 0) return `No open findings. ${reviews} raised no findings.`;
  const settled = page.findings === 1 ? "and it is settled" : "and every one is settled";
  return `No open findings. ${reviews} raised ${word(page.findings)} ${plural(page.findings, "finding")}, ${settled}.`;
}

function verdictOf(page: FindingsPage): { sentence: string; state: State } {
  const drafts =
    page.drafts === 0
      ? ""
      : page.drafts === 1
        ? " One follow-up becomes a Backlog draft when its PR merges."
        : ` ${capital(word(page.drafts))} follow-ups become Backlog drafts when their PRs merge.`;
  if (page.open.length === 0) return { sentence: settledSentence(page) + drafts, state: "ready_to_merge" };
  const state = page.open.some((each) => each.idle != null) ? "needs_you" : "running";
  return { sentence: openSentence(page.open) + drafts, state };
}

function Group({ label, empty, rows }: { label: string; empty: string; rows: ReactNode[] }) {
  const count = rows.length;
  return (
    <section aria-label={label} className="flex flex-col gap-3 px-5 py-4">
      <h2 className="flex items-center gap-2.5 font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em] text-ink-3">
        <span>{label}</span>
        {count > 0 && <span data-count className="text-ink">{String(count).padStart(2, "0")}</span>}
        <span aria-hidden="true" className="h-px flex-1 bg-rule-2" />
      </h2>
      {count === 0 ? <p className="font-mono text-[13px] text-ink-3">{empty}</p> : <ul>{rows}</ul>}
    </section>
  );
}

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

export function Findings() {
  const page = useFindingsPage();
  const { project } = page;
  const where = project == null ? "" : ` in ${project}`;
  const verdict = verdictOf(page);
  const findingRows = (list: { row: FindingRowView }[], quiet = false) =>
    list.map(({ row }) => <FindingRow key={row.key} row={row} showProject={project == null} quiet={quiet} />);
  const groups = [
    { label: "Open", empty: `Nothing open${where}.`, rows: findingRows(page.open) },
    { label: "Follow-ups", empty: `No follow-ups${where}.`, rows: findingRows(page.followUps) },
    { label: "Settled this week", empty: `Nothing settled${where} this week.`, rows: findingRows(page.settled, true) },
    {
      label: "Clean reviews",
      empty: `No clean reviews${where} this week.`,
      rows: page.clean.map((clean) => <CleanReviewLine key={`${clean.project} ${clean.run} ${clean.round}`} {...clean} />),
    },
  ];
  return (
    <section aria-labelledby="page-title" className="flex max-w-[1180px] flex-col gap-6 px-8 py-7">
      <header className="flex flex-col gap-2">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Findings</h1>
        {page.answered && (
          <>
            <p data-verdict className="flex items-start gap-2 font-plex text-[17px] leading-[1.45] text-ink">
              <span className="mt-[5px] shrink-0"><StateDot state={verdict.state} hideWord /></span>
              <span>{verdict.sentence}</span>
            </p>
            <p data-count-line className="font-mono text-[13px] [overflow-wrap:anywhere] text-ink-2">
              last 7 days
              <Slash />
              {page.reviews} {plural(page.reviews, "review")}
              <Slash />
              {page.findings} {plural(page.findings, "finding")}
              <Slash />
              worst first
            </p>
          </>
        )}
        {project != null && (
          <div data-scope-line className="mt-1 flex flex-wrap items-center gap-x-3.5 gap-y-2 font-mono text-[12.5px] text-ink-2">
            <span>
              Showing <b className="font-medium [overflow-wrap:anywhere] text-act">{project}</b> only
            </span>
            <Link
              to="/findings"
              search={{}}
              onClick={() => storeScope(null)}
              className="inline-flex min-h-8 items-center font-medium text-act underline underline-offset-2"
            >
              Show all projects
            </Link>
          </div>
        )}
      </header>
      {page.answered && (
        <div className="flex flex-col gap-2 border border-rule bg-paper py-2">
          {groups.map((group) => (
            <Group key={group.label} {...group} />
          ))}
        </div>
      )}
      <footer className="border-t border-rule pt-3 font-mono text-[12px] text-ink-3">
        Fates come from the fix commit: ADDRESS, FOLLOW_UP or DECLINE.
      </footer>
    </section>
  );
}
