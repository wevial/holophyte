import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { CHAT_SLOT, ScoutDocument, ScoutRail, type TabId } from "../components/ScoutBody";
import { ScoutHead } from "../components/ScoutHead";
import { ScoutStop } from "../components/ScoutStop";
import { ScoutThread } from "../components/ScoutThread";
import { scoutNotice, useScoutPage } from "../scout";
import { hasThread, takesMessages } from "../scoutThread";
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
  const { project, scout: param } = useParams({ from: "/scouts/$project/$scout" });
  const id = /^\d+$/.test(param) ? Number(param) : null;
  const page = useScoutPage(project, id);
  const heading = useRef<HTMLHeadingElement>(null);
  const stopSent = useRef(false);
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const chatButton = useRef<HTMLButtonElement>(null);
  const wide = useChatWidth();
  const [chatOpen, setChatOpen] = useState(readChatOpen);
  const [tab, setTab] = useState<{ at: string; id: TabId } | null>(null);
  const chatTitle = useId();
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
  const at = `${page.base}/${scout.id}`;
  const threaded = hasThread(page.detail);
  const docked = threaded && wide && chatOpen;
  const thread = threaded && <ScoutThread key={`${at}/thread`} base={page.base} detail={page.detail} actions={page.actions} ref={composeRef} />;
  const showChat = (open: boolean) => {
    setChatOpen(open);
    writeChatOpen(open);
  };
  const focusComposer = () => {
    composeRef.current?.focus();
    composeRef.current?.scrollIntoView({ block: "nearest" });
  };
  const askOrSteer = () => {
    flushSync(() => (wide ? showChat(true) : setTab({ at, id: "chat" })));
    focusComposer();
  };
  const closeChat = () => {
    flushSync(() => showChat(false));
    chatButton.current?.focus();
  };
  const chat = threaded
    ? { count: page.detail.thread.length, tab: wide ? null : thread, onOpen: wide && !chatOpen ? askOrSteer : null, openRef: chatButton }
    : null;
  return (
    <section aria-label={label} data-scout-layout
      className={`grid grid-cols-1 items-start min-[1100px]:grid-cols-[320px_minmax(0,1fr)] ${docked ? "min-[1440px]:grid-cols-[320px_minmax(0,1fr)_360px]" : ""}`}>
      <aside data-scout-rail aria-label="Summary and decisions"
        className="min-w-0 border-b border-rule bg-paper px-[22px] py-7 max-[720px]:p-4 min-[1100px]:sticky min-[1100px]:top-0 min-[1100px]:h-screen min-[1100px]:overflow-y-auto min-[1100px]:border-r min-[1100px]:border-b-0">
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
            {page.stoppable && <ScoutStop key={at} base={page.base} project={project} scout={scout.id} pass={page.pass?.live ? page.pass.pass : null} actions={page.actions} onSent={() => (stopSent.current = true)} />}
            {threaded && takesMessages(scout.phase) && <a href="#thread" className="btn" data-variant="quiet" onClick={(event) => {
              event.preventDefault();
              askOrSteer();
            }}>Ask or steer</a>}
          </div>
        </header>
        <ScoutRail page={page} project={project} />
      </aside>
      <div data-scout-body className="min-w-0 px-9 py-7 max-[720px]:p-4">
        <ScoutDocument key={at} page={page} chat={chat} chosen={tab?.at === at ? tab.id : null} onChoose={(id) => setTab({ at, id })} />
      </div>
      {docked && (
        <section data-scout-chat aria-labelledby={chatTitle}
          className="hidden min-w-0 flex-col border-l border-rule bg-paper min-[1440px]:sticky min-[1440px]:top-0 min-[1440px]:flex min-[1440px]:h-screen">
          <header className="flex items-center justify-between gap-2 border-b border-rule px-[18px] py-4">
            <h2 id={chatTitle} className="m-0 font-sans text-[14px] font-semibold text-ink">Chat with the scout</h2>
            <button type="button" aria-label="Close chat" title="Close chat" onClick={closeChat}
              className="inline-flex min-h-9 min-w-9 cursor-pointer items-center justify-center text-[18px] leading-none text-ink-3 hover:text-ink">
              ✕
            </button>
          </header>
          <div className={`flex min-h-0 flex-1 flex-col px-[18px] py-4 ${CHAT_SLOT} [&>section]:flex [&>section]:min-h-0 [&>section]:flex-1 [&>section]:flex-col [&>section>ol]:mt-0 [&>section>ol]:min-h-0 [&>section>ol]:overflow-y-auto [&>section>div]:mt-auto [&>section>div]:pt-4`}>
            {thread}
          </div>
        </section>
      )}
    </section>
  );
}
