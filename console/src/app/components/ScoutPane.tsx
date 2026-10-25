import { Link } from "@tanstack/react-router";
import { useLayoutEffect, useRef, useState } from "react";
import { scoutNotice, scoutPage, SCOUT_DOTS, useScoutPage } from "../scout";
import { hasThread } from "../scoutThread";
import { ClassicIcon } from "./icons";
import { ScoutActions } from "./ScoutActions";
import { ScoutFacts } from "./ScoutFacts";
import { ScoutReport } from "./ScoutReport";
import { ScoutThread } from "./ScoutThread";
import { StateDot } from "./StateDot";

/** The scout's sentence clamped to four lines, with More shown only when the clamp hides some of it. */
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
      <p ref={ref} role="status" className={`m-0 max-w-[76ch] font-sans text-[15px] leading-[1.55] text-ink ${open ? "" : "line-clamp-4"}`}>
        {text}
      </p>
      {(clipped || open) && (
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
          className="mt-1 cursor-pointer font-mono text-[12px] text-ink-2 underline underline-offset-2 hover:text-ink">
          {open ? "Less" : "More"}
        </button>
      )}
    </div>
  );
}

/** The wide Scouts page's pane: scout `id` of `project` condensed, its header, sentence, Action items, Report and Thread. */
export function ScoutPane({ project, id }: { project: string; id: number }) {
  const page = useScoutPage(project, id);
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const label = `scout ${id}`;
  if (page.kind !== "scout") {
    const notice = scoutNotice(page, project, String(id));
    return (
      <section aria-label={label} className="px-8 py-6">
        <p role={notice.loading ? "status" : undefined} className="m-0 font-mono text-[13px] text-ink-3">{notice.text}</p>
      </section>
    );
  }
  const { scout } = page.detail;
  const thread = hasThread(page.detail);
  const key = `${page.base}/${scout.id}`;
  return (
    <section aria-label={label} className="max-w-[960px] px-8 pb-8 pt-[22px]">
      <header>
        <div className="flex items-start justify-between gap-x-6 gap-y-2">
          <h2 className="m-0 max-w-[76ch] font-sans text-[17px] font-semibold leading-[1.4] text-ink [overflow-wrap:anywhere]">{scout.ask}</h2>
          <div className="flex shrink-0 gap-2">
            {thread && scout.phase === "done" && <a href="#thread" className="btn" data-variant="quiet" onClick={(event) => {
              event.preventDefault();
              composeRef.current?.focus();
              composeRef.current?.scrollIntoView({ block: "nearest" });
            }}>Ask or steer</a>}
            <Link {...scoutPage(project, scout.id)} aria-label={`Open scout ${scout.id}`} title={`Open scout ${scout.id}`} className="btn w-11 px-0" data-variant="quiet">
              <ClassicIcon />
            </Link>
          </div>
        </div>
        <p data-scout-facts className="m-0 mt-3 font-mono text-[12px] leading-[1.7] text-ink-3 [overflow-wrap:anywhere]">
          <ScoutFacts detail={page.detail} project={project} now={page.now} pass={page.pass} />
        </p>
        <div className="mt-4 flex items-start gap-3.5">
          <span className="mt-[3px]">
            <StateDot state={SCOUT_DOTS[page.state]} label={page.state} hideWord />
          </span>
          <Sentence key={key} text={page.sentence} />
        </div>
      </header>
      <ScoutActions key={`${key}/actions`} items={page.detail.actions} base={page.base} project={project} scout={scout.id} actions={page.actions} />
      <div className="min-[721px]:[&_[data-scout-finding]]:grid-cols-[2.375rem_6.75rem_minmax(0,1fr)]">
        <ScoutReport key={key} reports={page.detail.reports} baseSha={scout.base_sha} hypotheses={false} />
      </div>
      {thread && (
        <div className="mt-8 [&>aside]:px-0">
          <ScoutThread key={`${key}/thread`} base={page.base} detail={page.detail} actions={page.actions} ref={composeRef} />
        </div>
      )}
    </section>
  );
}
