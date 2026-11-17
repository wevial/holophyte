import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { formatAge } from "../../lib/format";
import { runPage } from "../run";
import { cardOf, STATES, type CardLook, type State } from "../state";
import { arrowPath, layoutGraph, type Box, type StoryNode } from "../story";
import { Card, cardSurface, type CardMark } from "./Card";
import { STATE_ICONS, TONE_TEXT } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";

const WAIT_STATES = new Set<State>(["on_ci", "parked", "queued", "held"]);

function markOf(node: StoryNode): CardMark | undefined {
  if (node.state === "failed") return "fail";
  if (node.kind === "need") return "need";
  return WAIT_STATES.has(node.state) ? "wait" : undefined;
}

const TITLE: Record<CardLook, string> = { merged: "text-ink-2", live: "text-ink", todo: "text-ink", backlog: "text-ink-3" };
const QUIET_WORD: Partial<Record<CardLook, string>> = { merged: "text-ink-3", backlog: "text-ink-3" };

const LEGEND: { look: CardLook; mark?: CardMark; icon?: State; words: string }[] = [
  { look: "merged", icon: "merged", words: "merged" },
  { look: "live", icon: "running", words: "in progress" },
  { look: "todo", icon: "todo", words: "to do" },
  { look: "backlog", icon: "backlog", words: "backlog" },
  { look: "todo", mark: "wait", words: "waiting" },
  { look: "todo", mark: "need", words: "needs you" },
  { look: "todo", mark: "fail", words: "failed" },
];

const OUTLINED: Partial<Record<State, ReactNode>> = {
  merged: (
    <>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M5 8.3 7.1 10.4 11 6.2" />
    </>
  ),
};

function Icon({ state }: { state: State }) {
  return (
    <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`size-3.5 shrink-0 ${TONE_TEXT[STATES[state].tone]}`}>
      {OUTLINED[state] ?? STATE_ICONS[state]}
    </svg>
  );
}

function Legend() {
  return (
    <div role="list" aria-label="Card states" className="mb-4 flex flex-wrap gap-x-[18px] gap-y-2 font-sans text-[12.5px] leading-[1.4] text-ink-2">
      {LEGEND.map((item) => (
        <span role="listitem" key={item.words} className="inline-flex items-center gap-1.5">
          <i aria-hidden="true" className={`mr-0.5 box-border inline-block h-3.5 w-[22px] rounded-[4px] ${cardSurface(item.look, item.mark)}`} />
          {item.icon != null && <Icon state={item.icon} />}
          {item.words}
        </span>
      ))}
    </div>
  );
}

function NodeCard({ node, project, className, after }: { node: StoryNode; project: string; className: string; after?: string }) {
  const look = cardOf(node.state);
  const waits = after ?? (node.depends_on.length > 0 ? `Waits on ${node.depends_on.join(", ")}` : "Starts the story");
  const label = `${node.ticket} ${plainTitle(node.title)}, ${node.word}. ${waits}`;
  const body = (
    <Card look={look} mark={markOf(node)}>
      <span className="flex items-center gap-2 font-mono text-[12px] leading-none text-ink-3 tabular-nums">
        <span>{node.ticket}</span>
        {node.time_box_ms != null && (
          <span data-time-box className="ml-auto inline-flex h-5 items-center rounded-[5px] bg-selected px-[7px] text-[11.5px] text-ink-2">
            {formatAge(node.time_box_ms)}
          </span>
        )}
      </span>
      <span className={`font-sans text-[13.5px] font-medium leading-[1.35] [overflow-wrap:anywhere] ${TITLE[look]}`}><TitleText>{node.title}</TitleText></span>
      <span data-state-line className="flex min-w-0 items-center gap-[5px] font-sans text-[12.5px] leading-[1.3]">
        <Icon state={node.state} />
        <span className={`truncate ${QUIET_WORD[look] ?? TONE_TEXT[STATES[node.state].tone]}`}>{node.word}</span>
      </span>
      {after != null && <span data-after className="font-sans text-[12px] leading-[1.3] text-ink-3">{after}</span>}
    </Card>
  );
  if (node.run == null) {
    return (
      <div role="group" aria-label={label} data-node={node.ticket} className={className}>
        {body}
      </div>
    );
  }
  return (
    <Link
      {...runPage(project, node.run)}
      aria-label={label}
      data-node={node.ticket}
      className={`${className} no-underline hover:[&>article]:bg-hover`}
    >
      {body}
    </Link>
  );
}

function Steps({ steps, project }: { steps: StoryNode[][]; project: string }) {
  return (
    <div className="story-steps flex flex-col gap-4">
      {steps.map((column, index) => (
        <section key={column[0]!.ticket} aria-label={`Step ${index + 1}`} data-step={index + 1}>
          <h3 className="flex items-baseline gap-2.5 font-sans text-[13px] font-semibold leading-none text-ink">
            {`Step ${index + 1}`}
            {column.length > 1 && <span className="text-[12px] font-normal text-ink-3">{column.length} can run side by side</span>}
          </h3>
          <ul className="mt-2 flex flex-wrap gap-2.5">
            {column.map((node) => (
              <li key={node.ticket} className="min-w-0 flex-[1_1_200px]">
                <NodeCard node={node} project={project} className="block [&>article]:h-full" after={node.depends_on.length > 0 ? `after ${node.depends_on.join(", ")}` : "starts the story"} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

const NO_BOX: Box = { left: 0, top: 0, width: 0, height: 0 };

function boxesIn(graph: HTMLElement): Record<string, Box> {
  const boxes: Record<string, Box> = {};
  for (const card of graph.querySelectorAll<HTMLElement>("[data-node]")) {
    boxes[card.dataset.node!] = { left: card.offsetLeft, top: card.offsetTop, width: card.offsetWidth, height: card.offsetHeight };
  }
  return boxes;
}

function stepNote(step: StoryNode[], index: number): string | null {
  if (step.length > 1) return `${step.length} side by side`;
  return index === 0 ? "starts the story" : null;
}

/** The story's children in rows by what they wait on, an arrow down from
 *  each dependency to the ticket that waits on it. */
export function StoryGraph({ nodes, project }: { nodes: StoryNode[]; project: string }) {
  const graph = layoutGraph(nodes);
  const edges = (dim: boolean) => graph.edges.filter((edge) => edge.dim === dim);
  const wide = useRef<HTMLDivElement>(null);
  const [boxes, setBoxes] = useState<Record<string, Box>>({});
  const readBoxes = () => {
    const region = wide.current;
    if (region == null) return;
    const next = boxesIn(region);
    setBoxes((was) => (JSON.stringify(was) === JSON.stringify(next) ? was : next));
  };
  useLayoutEffect(readBoxes);
  useEffect(() => {
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(readBoxes);
    observer.observe(wide.current!);
    return () => observer.disconnect();
  }, []);
  const path = (edge: { from: string; to: string }) => arrowPath(boxes[edge.from] ?? NO_BOX, boxes[edge.to] ?? NO_BOX);
  return (
    <section aria-label="Dependency graph" className="story-graph mt-5">
      <div className="story-wide">
        <Legend />
      </div>
      <div ref={wide} data-graph className="story-wide relative isolate flex flex-col">
        <svg
          aria-hidden="true"
          fill="none"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="pointer-events-none absolute left-0 top-0 z-[1] h-full w-full overflow-visible"
        >
          <g data-edges="dim" className="stroke-edge-dim">
            {edges(true).map((edge) => <path key={`${edge.from} ${edge.to}`} data-edge={`${edge.from} ${edge.to}`} d={path(edge)} />)}
          </g>
          <g data-edges="open" className="stroke-edge">
            {edges(false).map((edge) => <path key={`${edge.from} ${edge.to}`} data-edge={`${edge.from} ${edge.to}`} d={path(edge)} />)}
          </g>
        </svg>
        {graph.steps.map((step, index) => {
          const note = stepNote(step, index);
          return (
            <Fragment key={step[0]!.ticket}>
              {index > 0 && <div aria-hidden="true" data-band className="relative h-[52px] before:absolute before:inset-x-0 before:top-1/2 before:border-t before:border-dashed before:border-rule-2" />}
              <div role="group" aria-label={`Step ${index + 1}`} data-graph-step={index + 1} className="flex">
                <div className="flex w-[128px] shrink-0 flex-col gap-1.5 pr-2 pt-2.5">
                  <span className="font-sans text-[13px] font-semibold leading-none text-ink">{`Step ${index + 1}`}</span>
                  {note != null && <span data-step-note className="font-sans text-[12px] leading-[1.35] text-ink-3">{note}</span>}
                </div>
                <div data-step-cards className="flex min-w-0 flex-1 flex-wrap gap-x-14 gap-y-4">
                  {step.map((node) => (
                    <NodeCard key={node.ticket} node={node} project={project} className="relative z-[2] block min-h-[112px] w-[340px] [&>article]:h-full" />
                  ))}
                </div>
              </div>
            </Fragment>
          );
        })}
      </div>
      <Steps steps={graph.steps} project={project} />
      <p data-graph-key className="story-wide mt-3.5 flex flex-wrap items-center gap-x-[18px] gap-y-2 font-sans text-[12px] leading-[1.4] text-ink-3">
        <span className="inline-flex items-center gap-2">
          <svg width={34} height={10} viewBox="0 0 34 10" aria-hidden="true" fill="none" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" className="stroke-edge">
            <path d="M1 5h31M26 1l6 4-6 4" />
          </svg>
          the ticket at the arrow waits on the one it comes from
        </span>
        <span>one row runs in parallel</span>
        <span>faint arrows: both ends merged</span>
      </p>
    </section>
  );
}
