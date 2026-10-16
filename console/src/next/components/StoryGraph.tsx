import { useEffect, useRef, useState, type CSSProperties } from "react";
import { formatAge } from "../../lib/format";
import { classicHref } from "../../lib/view";
import { cardOf, STATES, type CardLook, type State } from "../state";
import { CARD_H, CARD_W, edgeFade, layoutGraph, type StoryNode } from "../story";
import { Card, cardSurface, type CardMark } from "./Card";
import { STATE_ICONS, TONE_TEXT } from "./StateDot";

const WAIT_STATES = new Set<State>(["on_ci", "parked", "queued", "held"]);

function markOf(node: StoryNode): CardMark | undefined {
  if (node.state === "failed") return "fail";
  if (node.kind === "need") return "need";
  return WAIT_STATES.has(node.state) ? "wait" : undefined;
}

const TITLE: Record<CardLook, string> = { merged: "text-ink-2", live: "text-ink", todo: "text-ink", backlog: "text-ink-3" };
const QUIET_WORD: Partial<Record<CardLook, string>> = { merged: "text-ink-3", backlog: "text-ink-3" };

const LEGEND: { look: CardLook; mark?: CardMark; icon?: State; words: string }[] = [
  { look: "merged", icon: "merged", words: "merged · green-tinted card, solid dot" },
  { look: "live", icon: "running", words: "in progress · ringed green dot and its stage" },
  { look: "todo", icon: "todo", words: "to do or waiting · full card, hollow ring" },
  { look: "backlog", icon: "backlog", words: "backlog · no surface, dashed, faded" },
  { look: "todo", mark: "wait", words: "queued, on CI, held or parked" },
  { look: "todo", mark: "need", words: "needs you" },
  { look: "todo", mark: "fail", words: "failed" },
];

function Icon({ state }: { state: State }) {
  return (
    <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`size-3.5 shrink-0 ${TONE_TEXT[STATES[state].tone]}`}>
      {STATE_ICONS[state]}
    </svg>
  );
}

function Legend() {
  return (
    <div role="list" aria-label="Card states" className="mb-3.5 flex flex-wrap gap-x-[18px] gap-y-2 font-mono text-[12px] leading-[1.4] text-ink-2">
      {LEGEND.map((item) => (
        <span role="listitem" key={item.words} className="inline-flex items-center gap-2">
          <i aria-hidden="true" className={`box-border inline-block h-3.5 w-[22px] ${cardSurface(item.look, item.mark)}`} />
          {item.icon != null && <Icon state={item.icon} />}
          {item.words}
        </span>
      ))}
    </div>
  );
}

function NodeCard({ node, base, className, style, after }: { node: StoryNode; base: string; className: string; style?: CSSProperties; after?: string }) {
  const look = cardOf(node.state);
  const waits = node.depends_on.length > 0 ? `Waits on ${node.depends_on.join(", ")}` : "Starts the story";
  const label = `${node.ticket} ${node.title}, ${node.word}. ${waits}`;
  const body = (
    <Card look={look} mark={markOf(node)}>
      <span className="flex items-center gap-2 font-mono text-[11px] font-medium leading-none text-ink-3 tabular-nums">
        <span>{node.ticket}</span>
        {node.time_box_ms != null && (
          <span data-time-box className="ml-auto inline-grid h-5 min-w-5 place-items-center px-1 text-ink-2 shadow-[inset_0_0_0_1px_var(--rule)]">
            {formatAge(node.time_box_ms)}
          </span>
        )}
      </span>
      <span className={`line-clamp-2 font-sans text-[14px] font-semibold leading-[1.3] [overflow-wrap:anywhere] ${TITLE[look]}`}>{node.title}</span>
      <span data-state-line className="flex min-w-0 items-center gap-[7px] font-mono text-[11.5px] font-medium leading-[1.3]">
        <Icon state={node.state} />
        <span className={`truncate ${QUIET_WORD[look] ?? TONE_TEXT[STATES[node.state].tone]}`}>{node.word}</span>
      </span>
      {after != null && <span data-after className="font-mono text-[11.5px] leading-[1.3] text-ink-3">{after}</span>}
    </Card>
  );
  if (node.run == null) {
    return (
      <div role="group" aria-label={label} data-node={node.ticket} className={className} style={style}>
        {body}
      </div>
    );
  }
  return (
    <a
      href={classicHref(`#run=${node.run}&daemon=${encodeURIComponent(base)}`)}
      aria-label={label}
      data-node={node.ticket}
      className={`${className} no-underline transition-colors duration-(--motion-colour) hover:[&>article]:border-ink-3`}
      style={style}
    >
      {body}
    </a>
  );
}

function Steps({ columns, base }: { columns: StoryNode[][]; base: string }) {
  return (
    <div className="story-steps flex flex-col gap-4">
      {columns.map((column, index) => (
        <section key={column[0]!.ticket} aria-label={`Step ${index + 1}`} data-step={index + 1}>
          <h3 className="flex items-baseline gap-2.5 font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em] text-ink-3">
            {`Step ${index + 1}`}
            {column.length > 1 && <span className="font-normal normal-case tracking-normal">{column.length} can run side by side</span>}
          </h3>
          <ul className="mt-2 flex flex-wrap gap-2.5">
            {column.map((node) => (
              <li key={node.ticket} className="min-w-0 flex-[1_1_200px]">
                <NodeCard node={node} base={base} className="block [&>article]:h-full" after={node.depends_on.length > 0 ? `after ${node.depends_on.join(", ")}` : "starts the story"} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

const FOCUS_INSET = 24;

/** The story's children as columns by what they wait on, an arrow from
 *  each dependency to the ticket that waits on it. */
export function StoryGraph({ nodes, base }: { nodes: StoryNode[]; base: string }) {
  const graph = layoutGraph(nodes);
  const edges = (dim: boolean) => graph.edges.filter((edge) => edge.dim === dim);
  const scroller = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState({ left: false, right: false });
  const readFade = () => {
    const region = scroller.current;
    if (region == null) return;
    const next = edgeFade(region.scrollLeft, region.clientWidth, region.scrollWidth);
    setFade((was) => (was.left === next.left && was.right === next.right ? was : next));
  };
  const focus = (nodes.find((node) => node.kind === "need") ?? nodes.find((node) => node.kind === "flight"))?.ticket;
  const focusX = useRef(focus == null ? null : graph.at.get(focus)!.x);
  useEffect(() => {
    const region = scroller.current;
    if (region == null) return;
    const read = () => {
      const x = focusX.current;
      if (x != null && region.clientWidth > 0) {
        focusX.current = null;
        if (x + CARD_W > region.scrollLeft + region.clientWidth) region.scrollLeft = Math.max(0, x - FOCUS_INSET);
      }
      readFade();
    };
    read();
    const observer = new ResizeObserver(read);
    observer.observe(region);
    return () => observer.disconnect();
  }, []);
  const fades = `${fade.left ? " fade-l" : ""}${fade.right ? " fade-r" : ""}`;
  return (
    <section aria-label="Dependency graph" className="story-graph mt-5 border border-rule bg-paper px-[18px] pb-5 pt-[18px]">
      <div className="story-wide">
        <Legend />
      </div>
      <div
        ref={scroller}
        role="region"
        aria-label="Story graph, scrolls sideways when wide"
        tabIndex={0}
        onScroll={readFade}
        className={`story-wide story-scroll overflow-x-auto overscroll-x-contain pb-1.5${fades}`}
      >
        <div className="relative" style={{ width: graph.width, height: graph.height }}>
          <svg
            aria-hidden="true"
            width={graph.width}
            height={graph.height}
            viewBox={`0 0 ${graph.width} ${graph.height}`}
            fill="none"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            className="pointer-events-none absolute left-0 top-0 overflow-visible"
          >
            <g data-edges="dim" className="stroke-edge-dim">
              {edges(true).map((edge) => <path key={`${edge.from} ${edge.to}`} data-edge={`${edge.from} ${edge.to}`} d={edge.path} />)}
            </g>
            <g data-edges="open" className="stroke-edge">
              {edges(false).map((edge) => <path key={`${edge.from} ${edge.to}`} data-edge={`${edge.from} ${edge.to}`} d={edge.path} />)}
            </g>
          </svg>
          {graph.columns.flat().map((node) => {
            const { x, y } = graph.at.get(node.ticket)!;
            return <NodeCard key={node.ticket} node={node} base={base} className="absolute block overflow-hidden [&>article]:h-full" style={{ left: x, top: y, width: CARD_W, height: CARD_H }} />;
          })}
        </div>
      </div>
      <Steps columns={graph.columns} base={base} />
      <p data-graph-key className="story-wide mt-3.5 flex flex-wrap items-center gap-x-[18px] gap-y-2 font-mono text-[12px] leading-[1.4] text-ink-3">
        <span className="inline-flex items-center gap-2">
          <svg width={34} height={10} viewBox="0 0 34 10" aria-hidden="true" fill="none" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" className="stroke-edge">
            <path d="M1 5h31M26 1l6 4-6 4" />
          </svg>
          the ticket at the arrow waits on the one it comes from
        </span>
        <span>one column runs in parallel</span>
        <span>faint arrows: both ends merged</span>
      </p>
    </section>
  );
}
