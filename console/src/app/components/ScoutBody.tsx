import { useLayoutEffect, useRef, useState, type ReactNode, type Ref } from "react";
import { formatSpan } from "../../lib/format";
import type { ScoutPage } from "../scout";
import { hasThread } from "../scoutThread";
import { SECTION_LABEL } from "./Criteria";
import { ScoutActions } from "./ScoutActions";
import { ScoutActivity } from "./ScoutActivity";
import { ScoutReport } from "./ScoutReport";
import { ScoutThread } from "./ScoutThread";
import { StateDot } from "./StateDot";

type Shown = Extract<ScoutPage, { kind: "scout" }>;

const CLAMPS = { 3: "line-clamp-3" } as const;

/** `text` clamped to `lines`, with More shown only when the clamp hides some of it. */
function Sentence({ text, lines }: { text: string; lines: keyof typeof CLAMPS }) {
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
      <p ref={ref} role="status" className={`m-0 max-w-[70ch] font-read text-[17px] leading-[1.5] text-ink ${open ? "" : CLAMPS[lines]}`}>
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
  const report = detail.scout.phase === "done" ? [...detail.reports].sort((a, b) => a.pass - b.pass).at(-1)?.report : undefined;
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
    body = <Sentence key={report.summary} text={report.summary} lines={3} />;
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
