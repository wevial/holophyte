import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode, type Ref } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { formatSpan } from "../../lib/format";
import { numberFindings, type ScoutPage } from "../scout";
import { hasThread } from "../scoutThread";
import { SECTION_LABEL } from "./Criteria";
import { ScoutActions } from "./ScoutActions";
import { ScoutActivity } from "./ScoutActivity";
import { ScoutReport } from "./ScoutReport";
import { ScoutThread } from "./ScoutThread";
import { ScoutWriteup } from "./ScoutWriteup";
import { StateDot } from "./StateDot";

type Shown = Extract<ScoutPage, { kind: "scout" }>;

const newestReport = (detail: ScoutDetail) => [...detail.reports].sort((a, b) => a.pass - b.pass).at(-1)?.report;

/** `text` clamped to three lines, with More shown only when the clamp hides some of it. */
function Sentence({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [open, setOpen] = useState(false);
  const [clipped, setClipped] = useState(false);
  useLayoutEffect(() => {
    const measure = () => {
      const node = ref.current;
      if (node != null && !open) setClipped(node.scrollHeight > node.clientHeight + 1);
    };
    measure();
    const node = ref.current;
    if (node == null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [text, open]);
  return (
    <div className="min-w-0">
      <p ref={ref} role="status" className={`m-0 max-w-[70ch] font-read text-[17px] leading-[1.5] text-ink ${open ? "" : "line-clamp-3"}`}>
        {text}
      </p>
      {(clipped || open) && (
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
          className="mt-2 cursor-pointer font-mono text-[12px] text-ink-2 underline underline-offset-2 hover:text-ink">
          {open ? "Less" : "More"}
        </button>
      )}
    </div>
  );
}

/** A done scout's answer from its newest report, its headline over its bullets or else its summary; any other scout's
 *  state sentence. */
function Verdict({ page }: { page: Shown }) {
  const { detail } = page;
  const report = detail.scout.phase === "done" ? newestReport(detail) : undefined;
  let body: ReactNode;
  if (report?.headline != null) {
    body = (
      <>
        <p role="status" className="m-0 max-w-[70ch] font-read text-[20px] font-semibold leading-[1.35] text-ink [overflow-wrap:anywhere]">{report.headline}</p>
        {report.bullets != null && report.bullets.length > 0 && (
          <ul className="m-0 mt-4 flex max-w-[70ch] list-none flex-col gap-2 p-0">
            {report.bullets.map((bullet, index) => (
              <li key={index} className="flex gap-4 font-read text-[15px] leading-[1.5] text-ink-2 [overflow-wrap:anywhere] before:mt-[0.6em] before:size-[5px] before:shrink-0 before:bg-ink-3 before:content-['']">
                {bullet}
              </li>
            ))}
          </ul>
        )}
      </>
    );
  } else if (report != null) {
    body = <Sentence key={report.summary} text={report.summary} />;
  } else {
    body = <p role="status" className="m-0 max-w-[70ch] font-read text-[17px] leading-[1.5] text-ink [overflow-wrap:anywhere]">{page.sentence}</p>;
  }
  return <section aria-label="Verdict">{body}</section>;
}

function Progress({ page }: { page: Shown }) {
  const { scout } = page.detail;
  const span = scout.started_ms == null || scout.ended_ms == null ? null : formatSpan(scout.ended_ms - scout.started_ms);
  return (
    <section aria-label="Progress" className="mt-10">
      <h2 className={`m-0 ${SECTION_LABEL}`}>
        Progress
        {page.live ? (
          <span data-live className="inline-flex items-center gap-1.5 tracking-[.1em] text-live">
            <span aria-hidden="true">
              <StateDot state="running" hideWord tone="live" />
            </span>
            live
          </span>
        ) : span != null && <span data-scout-span className="text-ink">{span}</span>}
      </h2>
      <ScoutActivity key={`${page.base}/${scout.id}`} groups={page.activity} now={page.live ? page.now : (scout.ended_ms ?? page.now)}
        stop={scout.phase === "swarming" ? { base: page.base, scout: scout.id, actions: page.actions } : null} />
    </section>
  );
}

/** A scout's body, shared by its page and the wide Scouts page's pane: the Verdict, Action items, Findings and
 *  Progress, with Progress second while the scout is live, then the Thread. */
export function ScoutBody({ page, project, composeRef }: { page: Shown; project: string; composeRef: Ref<HTMLTextAreaElement> }) {
  const { detail, base } = page;
  const { scout } = detail;
  const key = `${base}/${scout.id}`;
  const progress = <Progress key="progress" page={page} />;
  const found = [
    <ScoutActions key={`${key}/actions`} items={detail.actions} base={base} project={project} scout={scout.id} actions={page.actions} />,
    <ScoutReport key={`${key}/findings`} reports={detail.reports} baseSha={scout.base_sha} />,
  ];
  return (
    <>
      <Verdict page={page} />
      {page.live ? [progress, ...found] : [...found, progress]}
      {hasThread(detail) && <ScoutThread key={`${key}/thread`} base={base} detail={detail} actions={page.actions} ref={composeRef} />}
    </>
  );
}

/** A scout's page rail below its head: Summary, the Verdict, then Decide, its action items. */
export function ScoutRail({ page, project }: { page: Shown; project: string }) {
  const { detail, base } = page;
  const { scout } = detail;
  const summary = useId();
  return (
    <>
      <section aria-labelledby={summary} className="mt-8">
        <h2 id={summary} className={`m-0 ${SECTION_LABEL}`}>Summary</h2>
        <div className="mt-4"><Verdict page={page} /></div>
      </section>
      <div className="mt-8 empty:hidden">
        <ScoutActions key={`${base}/${scout.id}/actions`} title="Decide" compact items={detail.actions} base={base} project={project} scout={scout.id} actions={page.actions} />
      </div>
    </>
  );
}

type TabId = "writeup" | "findings" | "activity";

const TAB = "-mb-px inline-flex min-h-11 cursor-pointer items-center gap-2 border-0 border-b-2 bg-transparent px-4 font-sans text-[12px] font-medium uppercase tracking-[0.12em]";
const TAB_ON = "border-act bg-act-wash text-act";
const TAB_OFF = "border-transparent text-ink-2 hover:bg-side-hover hover:text-ink";

/** A scout's page document: tabs Write-up (when the newest report has one), Findings (when there is a report) and
 *  Activity, opening on the first of them, then the Thread. */
export function ScoutDocument({ page, composeRef }: { page: Shown; composeRef: Ref<HTMLTextAreaElement> }) {
  const { detail, base } = page;
  const { scout } = detail;
  const prefix = useId();
  const [chosen, setChosen] = useState<TabId | null>(null);
  const writeup = newestReport(detail)?.writeup ?? [];
  const tabs: { id: TabId; label: string; count?: number; body: ReactNode }[] = [
    ...(writeup.length > 0 ? [{ id: "writeup" as const, label: "Write-up", body: <ScoutWriteup sections={writeup} /> }] : []),
    ...(detail.reports.length > 0 ? [{
      id: "findings" as const, label: "Findings", count: numberFindings(detail.reports).length,
      body: <ScoutReport reports={detail.reports} baseSha={scout.base_sha} />,
    }] : []),
    { id: "activity", label: "Activity", body: <Progress page={page} /> },
  ];
  const current = tabs.find((tab) => tab.id === chosen) ?? tabs[0]!;
  const tabRefs = useRef(new Map<TabId, HTMLButtonElement>());
  const onKeyDown = (event: KeyboardEvent) => {
    const at = tabs.indexOf(current);
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 }[event.key];
    if (to == null) return;
    event.preventDefault();
    const next = tabs[(to + tabs.length) % tabs.length]!;
    setChosen(next.id);
    tabRefs.current.get(next.id)?.focus();
  };
  return (
    <>
      <div role="tablist" aria-label="Scout document" onKeyDown={onKeyDown} className="flex flex-wrap border-b border-rule">
        {tabs.map((tab) => (
          <button key={tab.id} ref={(node) => void (node ? tabRefs.current.set(tab.id, node) : tabRefs.current.delete(tab.id))}
            type="button" role="tab" id={`${prefix}-${tab.id}-tab`} aria-controls={`${prefix}-${tab.id}`} aria-selected={tab === current}
            tabIndex={tab === current ? 0 : -1} onClick={() => setChosen(tab.id)} className={`${TAB} ${tab === current ? TAB_ON : TAB_OFF}`}>
            {tab.label}
            {tab.count != null && " "}
            {tab.count != null && <span data-tab-count className="font-mono text-[11px] tracking-normal">{tab.count}</span>}
          </button>
        ))}
      </div>
      {tabs.map((tab) => (
        <div key={tab.id} role="tabpanel" id={`${prefix}-${tab.id}`} aria-labelledby={`${prefix}-${tab.id}-tab`} tabIndex={0} hidden={tab !== current}
          className="pt-8 [&>:first-child]:mt-0">
          {tab.body}
        </div>
      ))}
      {hasThread(detail) && <ScoutThread base={base} detail={detail} actions={page.actions} ref={composeRef} />}
    </>
  );
}
