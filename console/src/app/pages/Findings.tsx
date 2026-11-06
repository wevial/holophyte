import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { CleanReviewLine, FindingRow } from "../components/FindingRow";
import { dayLabel, dayRangeLabel, FilterChips, FilterPopover, RangeButtons, type FilterChip, type FilterSection } from "../components/ListFilters";
import { StateDot } from "../components/StateDot";
import { TicketDefectRow } from "../components/TicketDefectRow";
import {
  byProject, DEFAULT_FINDINGS_RANGE, facetCounts, FINDING_CATEGORIES, FINDING_STATUSES, filteredPage, SEVERITIES, useFindingsPage,
  type FindingCategory, type FindingRowView, type FindingsPage, type FindingsSearch, type FindingStatus, type Idle, type OpenFinding,
} from "../findings";
import { listOf } from "../history";
import type { FindingsRange } from "../queries";
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

function settledSentence(page: FindingsPage, span: string): string {
  if (page.reviews === 0) return `No reviews ${span}.`;
  const reviews = `${capital(word(page.reviews))} ${plural(page.reviews, "review")} ${span}`;
  if (page.findings === 0) return `No open findings. ${reviews} raised no findings.`;
  const settled = page.findings === 1 ? "and it is settled" : "and every one is settled";
  return `No open findings. ${reviews} raised ${word(page.findings)} ${plural(page.findings, "finding")}, ${settled}.`;
}

function verdictOf(page: FindingsPage, span: string): { sentence: string; state: State } {
  const drafts =
    page.drafts === 0
      ? ""
      : page.drafts === 1
        ? " One follow-up becomes a Backlog draft when its PR merges."
        : ` ${capital(word(page.drafts))} follow-ups become Backlog drafts when their PRs merge.`;
  if (page.open.length === 0) return { sentence: settledSentence(page, span) + drafts, state: "ready_to_merge" };
  const state = page.open.some((each) => each.idle != null) ? "needs_you" : "running";
  return { sentence: openSentence(page.open) + drafts, state };
}

interface Part {
  project: string | null;
  rows: ReactNode[];
}

const LABEL = "font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em]";

function ProjectPart({ project, rows }: Part) {
  if (project == null) return <ul>{rows}</ul>;
  return (
    <div data-project-group className="flex flex-col gap-2">
      <h3 className={`flex items-center gap-2 ${LABEL} text-ink-3`}>
        <span className="[overflow-wrap:anywhere]">{project}</span> <span className="text-ink">{rows.length}</span>
      </h3>
      <ul>{rows}</ul>
    </div>
  );
}

function Group({ label, empty, parts }: { label: string; empty: string; parts: Part[] }) {
  const count = parts.reduce((total, part) => total + part.rows.length, 0);
  return (
    <section aria-label={label} className="flex flex-col gap-3 px-5 py-4">
      <h2 className={`flex items-center gap-2.5 ${LABEL} text-ink-3`}>
        <span>{label}</span>
        {count > 0 && <span data-count className="text-ink">{String(count).padStart(2, "0")}</span>}
        <span aria-hidden="true" className="h-px flex-1 bg-rule-2" />
      </h2>
      {count === 0 ? <p className="font-mono text-[13px] text-ink-3">{empty}</p> : parts.map((part) => <ProjectPart key={part.project ?? ""} {...part} />)}
    </section>
  );
}

const RANGES: { value: FindingsRange; label: string; head: string; span: string }[] = [
  { value: "7d", label: "7d", head: "last 7 days", span: "this week" },
  { value: "30d", label: "30d", head: "last 30 days", span: "in the last 30 days" },
  { value: "90d", label: "90d", head: "last 90 days", span: "in the last 90 days" },
];

const STATUS_LABELS: Record<FindingStatus, string> = {
  open: "Open",
  follow_up: "Follow-up",
  fixed: "Fixed",
  declined: "Declined",
  clean: "Clean reviews",
  defect: "Ticket defects",
};

const CATEGORY_LABELS: Record<FindingCategory, string> = {
  correctness: "correctness",
  security: "security",
  tests: "tests",
  performance: "performance",
  maintainability: "maintainability",
  docs: "docs",
  uncategorized: "Uncategorized",
};

const CLEARED = { range: undefined, from: undefined, to: undefined, severity: undefined, category: undefined, status: undefined } satisfies FindingsSearch;

type Clears = Partial<typeof CLEARED>;

/** The count line's head and the phrase naming the window in sentences. */
function windowWords({ range, from, to }: FindingsSearch): { head: string; span: string } {
  if (from != null && to != null) return { head: dayRangeLabel(from, to), span: `from ${dayLabel(from)} to ${dayLabel(to)}` };
  if (from != null) return { head: dayRangeLabel(from, to), span: `since ${dayLabel(from)}` };
  if (to != null) return { head: dayRangeLabel(from, to), span: `up to ${dayLabel(to)}` };
  return RANGES.find((each) => each.value === (range ?? DEFAULT_FINDINGS_RANGE))!;
}

function chipsOf(search: FindingsSearch, sections: FilterSection[], head: string): FilterChip<Clears>[] {
  const chips: FilterChip<Clears>[] = sections.flatMap((section) => {
    const checked = listOf(search[section.param as "severity" | "category" | "status"]);
    const labels = section.options.filter((option) => checked.includes(option.value)).map((option) => option.label);
    return labels.length === 0 ? [] : [{ word: section.label, value: labels.join(", "), clears: { [section.param]: undefined } }];
  });
  if (search.range != null || search.from != null || search.to != null) chips.push({ word: "Range", value: head, clears: { range: undefined, from: undefined, to: undefined } });
  return chips;
}

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

export function Findings() {
  const search = useSearch({ from: "/findings" });
  const navigate = useNavigate();
  const all = useFindingsPage(search);
  const picked = { severity: listOf(search.severity), category: listOf(search.category), status: listOf(search.status) };
  const page = filteredPage(all, picked);
  const counts = facetCounts(all, picked);
  const { project } = page;
  const where = project == null ? "" : ` in ${project}`;
  const { head, span } = windowWords(search);
  const verdict = verdictOf(all, span);
  const sections: FilterSection[] = [
    { label: "Severity", param: "severity", options: SEVERITIES.map((severity) => ({ value: severity, label: severity, count: counts.severity[severity] })) },
    { label: "Category", param: "category", options: FINDING_CATEGORIES.map((category) => ({ value: category, label: CATEGORY_LABELS[category], count: counts.category[category] })) },
    { label: "Status", param: "status", options: FINDING_STATUSES.map((status) => ({ value: status, label: STATUS_LABELS[status], count: counts.status[status] })) },
  ];
  const chips = chipsOf(search, sections, head);
  const go = (clears: Clears) => void navigate({ to: ".", search: (previous: FindingsSearch) => ({ ...previous, ...clears }) });
  const split = <T,>(items: T[], projectOf: (item: T) => string, draw: (item: T) => ReactNode): Part[] =>
    project == null
      ? byProject(items, projectOf).map((part) => ({ project: part.project, rows: part.items.map(draw) }))
      : [{ project: null, rows: items.map(draw) }];
  const findingRows = (list: { row: FindingRowView }[], quiet = false) =>
    split(list, ({ row }) => row.project, ({ row }) => <FindingRow key={row.key} row={row} showProject={false} quiet={quiet} />);
  const groups = [
    { label: "Open", empty: `Nothing open${where}.`, parts: findingRows(page.open) },
    { label: "Follow-ups", empty: `No follow-ups${where}.`, parts: findingRows(page.followUps) },
    ...(all.defects.length === 0
      ? []
      : [
          {
            label: "Ticket defects",
            empty: `No ticket defects${where} ${span}.`,
            parts: split(page.defects, (defect) => defect.project, (defect) => <TicketDefectRow key={`${defect.base} ${defect.run} ${defect.at}`} defect={defect} />),
          },
        ]),
    { label: `Settled ${span}`, empty: `Nothing settled${where} ${span}.`, parts: findingRows(page.settled, true) },
    {
      label: "Clean reviews",
      empty: `No clean reviews${where} ${span}.`,
      parts: split(page.clean, (clean) => clean.project, (clean) => <CleanReviewLine key={`${clean.project} ${clean.run} ${clean.round}`} {...clean} />),
    },
  ];
  return (
    <section aria-labelledby="page-title" className="flex max-w-[1180px] flex-col gap-6 px-8 pt-3.5 pb-7">
      <header className="flex flex-col gap-2">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Findings</h1>
        {page.answered && (
          <>
            <p data-verdict className="flex items-start gap-2 font-read text-[17px] leading-[1.45] text-ink">
              <span className="mt-[5px] shrink-0"><StateDot state={verdict.state} hideWord /></span>
              <span>{verdict.sentence}</span>
            </p>
            <p data-count-line className="font-mono text-[13px] [overflow-wrap:anywhere] text-ink-2">
              {head}
              <Slash />
              {all.reviews} {plural(all.reviews, "review")}
              <Slash />
              {all.findings} {plural(all.findings, "finding")}
              <Slash />
              worst first
            </p>
          </>
        )}
        <div data-controls className="flex flex-col gap-2.5 pt-1">
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-2.5">
            <RangeButtons label="Range" presets={RANGES} fallback={DEFAULT_FINDINGS_RANGE} hidden={false} />
            <FilterPopover label="Filter findings" sections={sections} hidden={false} />
          </div>
          {chips.length > 0 && <FilterChips chips={chips} onClear={go} onClearAll={() => go(CLEARED)} />}
        </div>
        {project != null && (
          <div data-scope-line className="mt-1 flex flex-wrap items-center gap-x-3.5 gap-y-2 font-mono text-[12.5px] text-ink-2">
            <span>
              Showing <b className="font-medium [overflow-wrap:anywhere] text-act">{project}</b> only
            </span>
            <Link
              to="/findings"
              search={{ ...search, project: undefined }}
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
