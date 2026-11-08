import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal, flushSync } from "react-dom";
import { Btn } from "../components/Btn";
import { ChatIcon, FoldRightIcon } from "../components/icons";
import { CHAT_SLOT, ScoutDocument, Verdict, type TabId } from "../components/ScoutBody";
import { ScoutHead } from "../components/ScoutHead";
import { ScoutStop } from "../components/ScoutStop";
import { ScoutThread } from "../components/ScoutThread";
import { ScoutsBeside, ShowScoutList } from "../components/ScoutsSidebar";
import { scoutNotice, useScoutPage } from "../scout";
import { hasThread } from "../scoutThread";
import { useChatWidth } from "../viewport";
import { CRUMB_LINK, Loading, Notice, Page } from "./runShared";

const CHAT_KEY = "holophyte.next.scout-chat";

function readChatOpen(): boolean {
  try {
    return localStorage.getItem(CHAT_KEY) !== "closed";
  } catch {
    return true;
  }
}

function writeChatOpen(open: boolean): void {
  try {
    if (open) localStorage.removeItem(CHAT_KEY);
    else localStorage.setItem(CHAT_KEY, "closed");
  } catch {
    // Storage may be disabled; the sidebar still holds for this page.
  }
}

export function ScoutPage() {
  const { project, scout } = useParams({ from: "/scouts/$project/$scout" });
  const search = useSearch({ from: "/scouts/$project/$scout" });
  return (
    <ScoutsBeside search={search} current={`${project}/${scout}`}>
      <ScoutView project={project} param={scout} />
    </ScoutsBeside>
  );
}

/** Scout `param` of `project`: its report column and its chat's drawer. */
export function ScoutView({ project, param }: { project: string; param: string }) {
  const id = /^\d+$/.test(param) ? Number(param) : null;
  const page = useScoutPage(project, id);
  const heading = useRef<HTMLHeadingElement>(null);
  const stopSent = useRef(false);
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const chatButton = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();
  const wide = useChatWidth();
  const [chatOpen, setChatOpen] = useState(readChatOpen);
  const [tab, setTab] = useState<{ at: string; id: TabId } | null>(null);
  const chatTitle = useId();
  const drawerId = useId();
  const [threadHost] = useState(() => Object.assign(document.createElement("div"), { style: "display: contents" }));
  const holdThread = useCallback((slot: HTMLDivElement | null) => void slot?.append(threadHost), [threadHost]);
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
    return (
      <>
        <ShowScoutList row />
        {notice.loading ? <Page label={label}><Loading /></Page> : <Notice label={label} text={notice.text} back={{ to: "/scouts", search: { project } }} backLabel="Back to Scouts" />}
      </>
    );
  }
  const { scout } = page.detail;
  const at = `${page.base}/${scout.id}`;
  const threaded = hasThread(page.detail);
  const docked = threaded && wide && chatOpen;
  const thread = threaded && <ScoutThread key={`${at}/thread`} base={page.base} detail={page.detail} actions={page.actions} ref={composeRef} />;
  const showChat = (open: boolean) => {
    setChatOpen(open);
    writeChatOpen(open);
  };
  const openChat = () => {
    flushSync(() => showChat(true));
    composeRef.current?.focus();
  };
  const runAgain = () => void navigate({ to: "/scouts", search: {
    new: project, kind: scout.kind, ask: scout.ask, about: scout.subject == null ? undefined : `${scout.subject.kind}:${scout.subject.ref}`,
  } });
  const foldChat = () => {
    flushSync(() => showChat(false));
    chatButton.current?.focus();
  };
  return (
    <section aria-label={label} data-scout-layout
      className={`grid grid-cols-1 items-start ${docked ? "min-[1440px]:grid-cols-[minmax(0,1fr)_320px]" : ""}`}>
      <div data-scout-body className="min-w-0">
        <div data-scout-topbar className="flex min-h-14 flex-wrap items-center gap-x-2.5 gap-y-2 border-b border-rule px-7 py-2.5 max-[720px]:px-4">
          <ShowScoutList />
          <nav aria-label="Breadcrumb" className="flex-1 whitespace-nowrap font-mono text-[12px] leading-none text-ink-3">
            <Link to="/scouts" search={{}} className={CRUMB_LINK}>Scouts</Link>
            {" / "}
            <Link to="/scouts" search={{ project }} className={CRUMB_LINK}>{project}</Link>
            {" / "}
            <span>scout {scout.id}</span>
          </nav>
          {page.stoppable ? <ScoutStop key={at} base={page.base} project={project} scout={scout.id} pass={page.pass?.live ? page.pass.pass : null} actions={page.actions} onSent={() => (stopSent.current = true)} />
            : <Btn disabled={!page.actions} title={page.actions ? undefined : ACTIONS_OFF} onClick={runAgain}>Run again</Btn>}
          {threaded && wide && !chatOpen && (
            <button ref={chatButton} type="button" aria-expanded={false} aria-controls={drawerId} onClick={openChat} className="btn shrink-0 gap-1.5" data-variant="quiet">
              <ChatIcon />
              Chat
            </button>
          )}
        </div>
        <div className="flex max-w-[820px] flex-col gap-5 px-9 pt-7 pb-12 max-[720px]:p-4">
          <header>
            <ScoutHead detail={page.detail} state={page.state} now={page.now} headingRef={heading} />
          </header>
          <Verdict page={page} />
          <div>
            <ScoutDocument key={at} page={page} project={project} chat={threaded && !wide ? { count: page.detail.thread.length, slot: holdThread } : null}
              chosen={tab?.at === at ? tab.id : null} onChoose={(id) => setTab({ at, id })} />
          </div>
        </div>
      </div>
      {docked && (
        <section id={drawerId} data-scout-chat aria-labelledby={chatTitle}
          className="sticky top-0 hidden h-screen min-w-0 flex-col border-l border-rule bg-paper min-[1440px]:flex">
          <header className="flex min-h-14 items-center justify-between gap-2 border-b border-rule py-2.5 pr-3 pl-[18px]">
            <h2 id={chatTitle} className="m-0 font-sans text-[15px] font-semibold text-ink">Chat with the scout</h2>
            <button type="button" aria-label="Hide chat" title="Hide chat" aria-expanded={true} aria-controls={drawerId} onClick={foldChat}
              className="btn w-9 shrink-0 px-0" data-variant="quiet">
              <FoldRightIcon />
            </button>
          </header>
          <div ref={holdThread} className={`flex min-h-0 flex-1 flex-col px-[18px] py-4 ${CHAT_SLOT} [&>div>section]:flex [&>div>section]:min-h-0 [&>div>section]:flex-1 [&>div>section]:flex-col [&>div>section>ol]:mt-0 [&>div>section>ol]:min-h-0 [&>div>section>ol]:overflow-y-auto [&>div>section>div]:mt-auto [&>div>section>div]:pt-4`} />
        </section>
      )}
      {thread && createPortal(thread, threadHost)}
    </section>
  );
}
