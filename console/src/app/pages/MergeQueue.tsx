import { useContext, type ReactNode } from "react";
import { useSearch } from "@tanstack/react-router";
import type { MergeQueue } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { formatClock } from "../../lib/format";
import { LaneHead } from "../components/LaneHead";
import { LeftTheQueue, QueueTable } from "../components/MergeQueueTable";
import type { QueueDaemon } from "../components/QueueActions";
import { clearsAt, queueCounts } from "../mergeQueue";
import { useMergeQueues } from "../queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "../scope";
import { zoneName } from "./runShared";

const PANEL = "m-0 border border-rule bg-paper px-5 py-[18px] font-mono text-[13px] leading-[1.4]";

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

/** `4 queued / 1 building / clears ~16:29`, the clear time only when known. */
function Counts({ queued, building, clears }: { queued: number; building: number; clears: number | null }) {
  return (
    <>
      <b className="font-semibold text-ink">{queued}</b> queued
      <Slash />
      <b className="font-semibold text-ink">{building}</b> building
      {clears != null && <><Slash />clears ~{formatClock(clears)}</>}
    </>
  );
}

function QueueCounts({ queue }: { queue: MergeQueue }) {
  return <Counts {...queueCounts(queue)} clears={clearsAt(queue)} />;
}

function QueueBody({ queue, project, daemon }: { queue: MergeQueue; project: string; daemon: QueueDaemon }) {
  if (queue.configured === false) return <p className={`${PANEL} text-ink-2`}>No merge queue on this repository.</p>;
  if (queue.configured == null) return <p className={`${PANEL} text-bad`}>{queue.error ?? "The merge queue could not be read."}</p>;
  return (
    <div className="flex flex-col gap-8">
      <QueueTable queue={queue} project={project} daemon={daemon} />
      <LeftTheQueue left={queue.left} project={project} />
    </div>
  );
}

function TimesShown({ now }: { now: number }) {
  return <p className="font-mono text-[12px] leading-[1.4] text-ink-3">times show in {zoneName(now)}</p>;
}

function Header({ children }: { children: ReactNode }) {
  return (
    <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
      <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Merge queue</h1>
      <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">{children}</p>
    </header>
  );
}

interface Answer {
  name: string;
  project: string;
  key: string;
  queue: MergeQueue;
  daemon: QueueDaemon;
}

/** One lane per answer with a queue or an error, the answers without a
 *  queue named on one quiet line under them. */
function Lanes({ title, answers }: { title: ReactNode; answers: Answer[] }) {
  const lanes = answers.filter(({ queue }) => queue.configured !== false);
  const without = answers.filter(({ queue }) => queue.configured === false).map(({ name }) => name);
  const queues = lanes.filter(({ queue }) => queue.configured === true).map(({ queue }) => queue);
  const clears = queues.map(clearsAt).filter((at) => at != null);
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-4">
      <Header>
        {title} · <Counts
          queued={queues.reduce((sum, queue) => sum + queue.entries.length, 0)}
          building={queues.reduce((sum, queue) => sum + queueCounts(queue).building, 0)}
          clears={clears.length > 0 ? Math.max(...clears) : null}
        />
      </Header>
      {lanes.map(({ name, project, key, queue, daemon }) => (
        <div key={key} role="group" aria-label={name} data-queue-lane className="flex flex-col gap-3">
          <LaneHead name={name} held={false} attached={false} meta={queue.branch}>
            {queue.configured === true && <span><QueueCounts queue={queue} /></span>}
          </LaneHead>
          <QueueBody queue={queue} project={project} daemon={daemon} />
        </div>
      ))}
      {without.length > 0 && <p data-no-queue className="font-mono text-[12px] leading-[1.4] text-ink-3">No merge queue on {without.join(", ")}.</p>}
      {queues[0] && <TimesShown now={queues[0].now} />}
    </section>
  );
}

export function MergeQueuePage() {
  const { hosts } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const deps = useContext(PollDepsContext);
  const answers = useMergeQueues(scopedHosts(hosts, project), deps).map(({ host, queue }) => {
    const name = projectName(host.status?.project ?? host.project ?? host.label);
    const daemon = { project: name, base: host.base, actions: host.status?.actions === true };
    return { name: project == null ? name : host.label, project: name, key: host.key, queue, daemon };
  });
  if (project == null) return <Lanes title="all projects" answers={answers} />;
  if (answers.length > 1 && answers.some(({ queue }) => queue.configured !== false)) return <Lanes title={project} answers={answers} />;
  const queue = answers[0]?.queue;
  const daemon = answers[0]?.daemon;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-4">
      <Header>
        {project}
        {queue && <> · {queue.branch}</>}
        {queue?.configured === true && <> · <QueueCounts queue={queue} /></>}
      </Header>
      {queue && daemon && <QueueBody queue={queue} project={project} daemon={daemon} />}
      {queue?.configured === true && <TimesShown now={queue.now} />}
    </section>
  );
}
