import { Link } from "@tanstack/react-router";
import { useRef } from "react";
import { Loading } from "../pages/runShared";
import { scoutNotice, scoutPage, useScoutPage } from "../scout";
import { hasThread } from "../scoutThread";
import { ClassicIcon } from "./icons";
import { ScoutBody } from "./ScoutBody";
import { ScoutHead } from "./ScoutHead";

/** The wide Scouts page's pane: scout `id` of `project` with the same header and body as its page. */
export function ScoutPane({ project, id }: { project: string; id: number }) {
  const page = useScoutPage(project, id);
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const label = `scout ${id}`;
  if (page.kind !== "scout") {
    const notice = scoutNotice(page, project, String(id));
    return (
      <section aria-label={label} className="px-10 py-10">
        {notice.loading ? <Loading /> : <p className="m-0 font-mono text-[13px] text-ink-3">{notice.text}</p>}
      </section>
    );
  }
  const { scout } = page.detail;
  return (
    <section aria-label={label} className="max-w-[1000px] p-10 max-[720px]:p-4">
      <header className="border-b border-rule pb-6">
        <ScoutHead detail={page.detail} project={project} state={page.state} now={page.now} pass={page.pass} level={2} buttons={
          <>
            {hasThread(page.detail) && scout.phase === "done" && <a href="#thread" className="btn" data-variant="quiet" onClick={(event) => {
              event.preventDefault();
              composeRef.current?.focus();
              composeRef.current?.scrollIntoView({ block: "nearest" });
            }}>Ask or steer</a>}
            <Link {...scoutPage(project, scout.id)} aria-label={`Open scout ${scout.id}`} title={`Open scout ${scout.id}`} className="btn w-11 px-0" data-variant="quiet">
              <ClassicIcon />
            </Link>
          </>
        } />
      </header>
      <div data-scout-body className="mt-10">
        <ScoutBody page={page} project={project} composeRef={composeRef} />
      </div>
    </section>
  );
}
