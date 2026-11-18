import { useContext, type ReactNode } from "react";
import { useSearch } from "@tanstack/react-router";
import type { MergeQueue } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { formatClock } from "../../lib/format";
import { LeftTheQueue, QueueTable } from "../components/MergeQueueTable";
import { PageHeader } from "../components/PageHeader";
import type { QueueDaemon } from "../components/QueueActions";
import { Section } from "../components/Section";
import { clearsAt, queueCounts } from "../mergeQueue";
import { useMergeQueues } from "../queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "../scope";
import { zoneName } from "./runShared";

const LINE = "m-0 font-sans text-[13px] leading-[1.4]";

/** `4 queued · 1 building · clears ~16:29` over every readable queue, the clear time only when known. */
function countText(queues: MergeQueue[]): { text: string } {
  const queued = queues.reduce((sum, queue) => sum + queue.entries.length, 0);
  const building = queues.reduce((sum, queue) => sum + queueCounts(queue).building, 0);
  const clears = queues.map(clearsAt).filter((at) => at != null);
  const words = [`${queued} queued`, `${building} building`];
  if (clears.length > 0) words.push(`clears ~${formatClock(Math.max(...clears))}`);
  return { text: words.join(" · ") };
}

function Unread({ queue }: { queue: MergeQueue }) {
  return <p className={`${LINE} text-bad`}>{queue.error ?? "The merge queue could not be read."}</p>;
}

function QueueBody({ queue, project, daemon, label, lead }: { queue: MergeQueue; project: string; daemon: QueueDaemon; label: string; lead?: string }) {
  return (
    <div className="flex flex-col gap-8">
      <QueueTable queue={queue} project={project} daemon={daemon} label={label} lead={lead} />
      <LeftTheQueue left={queue.left} project={project} />
    </div>
  );
}

function TimesShown({ now }: { now: number }) {
  return <p className="font-sans text-[12px] leading-[1.4] text-ink-3">times show in {zoneName(now)}</p>;
}

function Page({ queues, counted, children }: { queues: MergeQueue[]; counted: boolean; children: ReactNode }) {
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-4">
      <PageHeader title="Merge queue" count={counted ? countText(queues) : undefined} />
      {children}
      {queues[0] && <TimesShown now={queues[0].now} />}
    </section>
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
function Lanes({ answers }: { answers: Answer[] }) {
  const lanes = answers.filter(({ queue }) => queue.configured !== false);
  const without = answers.filter(({ queue }) => queue.configured === false).map(({ name }) => name);
  const queues = lanes.filter(({ queue }) => queue.configured === true).map(({ queue }) => queue);
  return (
    <Page queues={queues} counted>
      {lanes.map(({ name, project, key, queue, daemon }) => (
        <div key={key} data-queue-lane className="flex flex-col gap-2">
          {queue.configured === true ? (
            <QueueBody queue={queue} project={project} daemon={daemon} label={name} lead={queue.branch} />
          ) : (
            <>
              <Section label={name} empty="" />
              <Unread queue={queue} />
            </>
          )}
        </div>
      ))}
      {without.length > 0 && <p data-no-queue className={`${LINE} text-ink-3`}>No merge queue on {without.join(", ")}.</p>}
    </Page>
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
  if (project == null) return <Lanes answers={answers} />;
  if (answers.length > 1 && answers.some(({ queue }) => queue.configured !== false)) return <Lanes answers={answers} />;
  const queue = answers[0]?.queue;
  const daemon = answers[0]?.daemon;
  return (
    <Page queues={queue?.configured === true ? [queue] : []} counted={queue?.configured === true}>
      {queue?.configured === false && <p className={`${LINE} text-ink-3`}>No merge queue on this repository.</p>}
      {queue?.configured == null && queue && <Unread queue={queue} />}
      {queue?.configured === true && daemon && <QueueBody queue={queue} project={project} daemon={daemon} label={queue.branch} />}
    </Page>
  );
}
