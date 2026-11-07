import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { ScoutDocument, ScoutRail } from "../components/ScoutBody";
import { ScoutHead } from "../components/ScoutHead";
import { ScoutStop } from "../components/ScoutStop";
import { scoutNotice, useScoutPage } from "../scout";
import { hasThread, takesMessages } from "../scoutThread";
import { CRUMB_LINK, Loading, Notice, Page } from "./runShared";

export function ScoutPage() {
  const { project, scout: param } = useParams({ from: "/scouts/$project/$scout" });
  const id = /^\d+$/.test(param) ? Number(param) : null;
  const page = useScoutPage(project, id);
  const heading = useRef<HTMLHeadingElement>(null);
  const stopSent = useRef(false);
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const stoppable = page.kind === "scout" && page.stoppable;
  useEffect(() => {
    if (stoppable || !stopSent.current) return;
    stopSent.current = false;
    if (document.activeElement === document.body) heading.current?.focus();
  }, [stoppable]);
  const shown = id == null ? param : String(id);
  const label = `scout ${shown}`;
  if (page.kind !== "scout") {
    const notice = scoutNotice(page, project, shown);
    if (!notice.loading) return <Notice label={label} text={notice.text} back={{ to: "/scouts", search: { project } }} backLabel="Back to Scouts" />;
    return <Page label={label}><Loading /></Page>;
  }
  const { scout } = page.detail;
  return (
    <Page label={label}>
      <div data-scout-layout className="grid grid-cols-1 items-start gap-6 min-[1100px]:grid-cols-[320px_minmax(0,1fr)]">
        <aside data-scout-rail aria-label="Summary and decisions"
          className="min-w-0 border border-rule bg-paper p-6 max-[720px]:p-4 min-[1100px]:sticky min-[1100px]:top-4 min-[1100px]:max-h-[calc(100vh-2rem)] min-[1100px]:overflow-y-auto">
          <header>
            <nav aria-label="Breadcrumb" className="mb-2 font-mono text-[12px] leading-none text-ink-3">
              <Link to="/scouts" search={{}} className={CRUMB_LINK}>Scouts</Link>
              {" / "}
              <Link to="/scouts" search={{ project }} className={CRUMB_LINK}>{project}</Link>
              {" / "}
              <span>scout {scout.id}</span>
            </nav>
            <ScoutHead detail={page.detail} project={project} state={page.state} now={page.now} pass={page.pass} level={1} headingRef={heading} buttons={null} />
            <div data-scout-buttons className="mt-4 flex flex-wrap gap-2 empty:hidden">
              {page.stoppable && <ScoutStop key={`${page.base}/${scout.id}`} base={page.base} project={project} scout={scout.id} pass={page.pass?.live ? page.pass.pass : null} actions={page.actions} onSent={() => (stopSent.current = true)} />}
              {hasThread(page.detail) && takesMessages(scout.phase) && <a href="#thread" className="btn" data-variant="quiet" onClick={(event) => {
                event.preventDefault();
                composeRef.current?.focus();
                composeRef.current?.scrollIntoView({ block: "nearest" });
              }}>Ask or steer</a>}
            </div>
          </header>
          <ScoutRail page={page} project={project} />
        </aside>
        <div data-scout-body className="min-w-0 border border-rule bg-paper p-10 max-[720px]:p-4">
          <ScoutDocument key={`${page.base}/${scout.id}`} page={page} composeRef={composeRef} />
        </div>
      </div>
    </Page>
  );
}
