import { useContext, useEffect } from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { StateDot } from "../components/StateDot";
import { storeScope } from "../projects";
import { useBoardStories, useReadiness, useStory } from "../queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers } from "../scope";
import { KIND_ORDER, storyNodes, storyState, type NodeKind, type StoryNode } from "../story";

const SEGMENT: Record<NodeKind, string> = {
  done: "bg-ink-3",
  flight: "bg-live",
  need: "bg-need",
  ready: "bg-wait",
  backlog: "border border-rule",
};

const STORY_HINT = "A story is a parent ticket with children. File one with child tickets and its graph shows here.";

function countsLine(nodes: StoryNode[]): string {
  const flight = nodes.filter((node) => node.kind === "flight").length;
  const need = nodes.filter((node) => node.kind === "need").length;
  const parts = [
    ...(flight > 0 ? [`${flight} in flight`] : []),
    ...(need > 0 ? [`${need} ${need === 1 ? "needs" : "need"} you`] : []),
  ];
  return parts.join(" · ");
}

function Header({ project, storyKey, title, nodes }: { project: string; storyKey: string; title: string; nodes: StoryNode[] }) {
  const chip = storyState(nodes);
  const merged = nodes.filter((node) => node.kind === "done").length;
  const segments = KIND_ORDER.flatMap((kind) => nodes.filter((node) => node.kind === kind));
  const counts = countsLine(nodes);
  return (
    <header className="flex flex-col border-b border-rule pb-3 pt-3">
      <nav aria-label="Breadcrumb" className="font-mono text-[12px] leading-none [overflow-wrap:anywhere] text-ink-3">
        <Link to="/tickets" search={{ project }} className="border-b border-rule text-ink-3 hover:border-ink hover:text-ink">
          {project} tickets
        </Link>
        {" / Stories / "}
        {storyKey}
      </nav>
      <h1 id="page-title" className="mt-2 font-sans text-[20px] font-semibold leading-[1.25] [overflow-wrap:anywhere] text-ink">{title}</h1>
      <div className="mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-2.5">
        <span data-story-chip className="inline-flex min-h-[30px] items-center gap-2 whitespace-nowrap border border-rule bg-paper-2 px-2.5 font-mono text-[12px] font-medium text-ink">
          <StateDot state={chip.state} hideWord />
          <span>{chip.word}</span>
        </span>
        <span data-merged className="inline-flex items-center gap-2.5 font-mono text-[13px] leading-none text-ink-2">
          <span>
            <b className="font-semibold tabular-nums text-ink">{merged} of {nodes.length}</b> merged
          </span>
          <span aria-hidden="true" className="inline-flex flex-wrap gap-[3px]">
            {segments.map((node) => (
              <span key={node.ticket} data-segment={node.kind} className={`box-border block h-2 w-[18px] ${SEGMENT[node.kind]}`} />
            ))}
          </span>
        </span>
        {counts !== "" && <span data-counts className="font-mono text-[13px] leading-none text-ink-3">{counts}</span>}
      </div>
    </header>
  );
}

function Missing({ project, storyKey }: { project: string; storyKey: string }) {
  return (
    <section aria-labelledby="page-title" className="mt-3 border border-rule bg-paper px-6 py-7">
      <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] [overflow-wrap:anywhere] text-ink">
        No story {storyKey} in {project}.
      </h1>
      <p className="mt-2.5 max-w-[60ch] text-[14px] text-ink-2">{STORY_HINT}</p>
    </section>
  );
}

export function Story() {
  const { project, key } = useParams({ from: "/stories/$project/$key" });
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { host, story } = useStory(peers.hosts, project, key, deps);
  const readiness = useReadiness(host == null ? [] : [host], deps);
  const missing = story === null || (host == null && peers.polls > 0);
  return (
    <div className="mx-auto flex w-full max-w-[1320px] flex-col px-8 py-7">
      {story != null && host != null && (
        <section aria-labelledby="page-title">
          <Header project={project} storyKey={key} title={story.title} nodes={storyNodes(story, host, peers.now, readiness)} />
        </section>
      )}
      {missing && <Missing project={project} storyKey={key} />}
      <p className="mt-3.5 border-t border-rule py-3.5 font-mono text-[12px] leading-[1.4] text-ink-3">
        Tickets with no dependency start the graph on the left. Pick another story from Stories in the sidebar.
      </p>
    </div>
  );
}

function NoStories({ project }: { project: string | undefined }) {
  return (
    <section aria-labelledby="page-title" className="border border-rule bg-paper px-6 py-7">
      <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] [overflow-wrap:anywhere] text-ink">
        No stories in {project ?? "all projects"}
      </h1>
      <p className="mt-2.5 max-w-[60ch] text-[14px] text-ink-2">{STORY_HINT}</p>
      {project != null && (
        <Link
          to="/stories"
          search={{}}
          onClick={() => storeScope(null)}
          className="mt-3.5 inline-flex min-h-8 items-center font-mono text-[12.5px] font-medium text-act underline underline-offset-2"
        >
          Show all projects
        </Link>
      )}
    </section>
  );
}

export function StoriesIndex() {
  const { project } = useSearch({ from: "__root__" });
  const deps = useContext(PollDepsContext);
  const { hosts, polls } = useConsolePeers();
  const boards = useBoardStories(scopedHosts(hosts, project), deps);
  const answered = polls > 0 && boards.answered;
  const { stories } = boards;
  const navigate = useNavigate();
  const first = stories[0];
  const firstProject = first?.project;
  const firstKey = first?.ticket;
  useEffect(() => {
    if (!answered || firstProject == null || firstKey == null) return;
    void navigate({ to: "/stories/$project/$key", params: { project: firstProject, key: firstKey }, search: keepScope, replace: true });
  }, [answered, firstProject, firstKey, navigate]);
  return (
    <div className="mx-auto flex w-full max-w-[1320px] flex-col px-8 py-7">
      {answered && first == null && <NoStories project={project} />}
    </div>
  );
}
