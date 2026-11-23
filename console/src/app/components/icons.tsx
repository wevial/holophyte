import type { RunCriterion } from "../../lib/contract.gen";
import type { ReactNode } from "react";

function Stroke({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width={16}
      height={16}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={`size-4 shrink-0 ${className ?? ""}`}
    >
      {children}
    </svg>
  );
}

const LEAF = "M1.43 15.18L1.94 15.6L2.42 15.14L3.78 13.63L6 10.66L6.65 10.37L7.36 10.2L11.09 9.9L12.3 9.7L13.62 9.36L14.42 8.68L14.58 8.04L14.43 7.1L14.19 6.66L13.3 5.44L12.51 3.67L12.21 3.57L11.76 3.61L11.18 3.8L10.47 4.17L10.36 4.13L10.63 2.64L10.59 2.12L10.42 1.8L9.5 1.5L7.93 1.69L7.37 1.63L6.91 1.42L6.15 0.88L5.74 0.63L4.96 0.4L4.44 0.46L2.81 0.95L2.43 1.1L2.18 1.34L1.98 1.86L1.95 2.57L2.35 3.86L2.83 5.02L4.72 8.71L4.98 9.45L5.07 10.14L4.56 11.27L3.7 12.57L2.59 13.96ZM9.25 10.03L8.03 10.1L9.43 9.5L11.32 8.43L13.02 7.14L13.9 6.31L14.14 6.62L13.28 7.46L11.55 8.77ZM9.44 6.25L7.1 9.33L6.8 9.06L8.49 6.9L10.37 3.95L10.29 4.28L10.8 4.01ZM5.65 4.69L5.32 8.49L4.91 8.49L5.24 4.63L6.03 0.82L6.39 1.08Z";

/** The pleated ginkgo of the app icon in the surrounding ink; even-odd filling cuts the pleat lines out of the leaf. */
export function Mark() {
  return (
    <svg viewBox="0 0 16 16" width={20} height={20} aria-hidden="true" className="size-5 shrink-0">
      <path d={LEAF} fill="currentColor" fillRule="evenodd" />
    </svg>
  );
}

export function AllProjectsIcon() {
  return (
    <Stroke>
      <rect x="2" y="2" width="5" height="5" />
      <rect x="9" y="2" width="5" height="5" />
      <rect x="2" y="9" width="5" height="5" />
      <rect x="9" y="9" width="5" height="5" />
    </Stroke>
  );
}

export function OverviewIcon() {
  return (
    <Stroke>
      <path d="M2 9.5h3.5l1 2h3l1-2H14" />
      <path d="M3.6 3h8.8L14 9.5V13H2V9.5Z" />
    </Stroke>
  );
}

export function RunsIcon() {
  return (
    <Stroke>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M6.6 5.4v5.2L10.8 8Z" />
    </Stroke>
  );
}

export function TicketsIcon() {
  return (
    <Stroke>
      <path d="M3.5 1.5h6l3 3v10h-9Z" />
      <path d="M9.5 1.5v3h3M5.8 8h4.4M5.8 10.8h4.4" />
    </Stroke>
  );
}

export function FindingsIcon() {
  return (
    <Stroke>
      <path d="M3.5 14.5V2M3.5 2.5h8.5l-2 3 2 3H3.5" />
    </Stroke>
  );
}

export function ScoutsIcon() {
  return (
    <Stroke>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M10.6 5.4 9 9 5.4 10.6 7 7Z" />
    </Stroke>
  );
}

export function ChevronIcon({ folded }: { folded: boolean }) {
  return (
    <svg
      viewBox="0 0 10 10"
      width={12}
      height={12}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      aria-hidden="true"
      className={`size-3 shrink-0 ${folded ? "-scale-x-100" : ""}`}
    >
      <path d="M6.5 2 3.5 5 6.5 8" />
    </svg>
  );
}

export function PanelLeftIcon() {
  return (
    <Stroke>
      <rect x="2" y="2.5" width="12" height="11" rx="2" />
      <path d="M6 2.5v11" />
    </Stroke>
  );
}

export function ChatIcon() {
  return (
    <Stroke>
      <path d="M2.5 3h11v7.5H6l-3.5 3z" />
    </Stroke>
  );
}

export function FoldRightIcon() {
  return (
    <Stroke>
      <path d="M7.5 4l4 4-4 4M3.5 4l4 4-4 4" />
    </Stroke>
  );
}

export function MoonIcon() {
  return (
    <Stroke>
      <path d="M13 9.6A5.5 5.5 0 1 1 6.4 3a4.4 4.4 0 0 0 6.6 6.6Z" />
    </Stroke>
  );
}

export function SunIcon() {
  return (
    <Stroke>
      <circle cx="8" cy="8" r="2.75" />
      <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.06 1.06M11.54 11.54l1.06 1.06M3.4 12.6l1.06-1.06M11.54 4.46l1.06-1.06" />
    </Stroke>
  );
}

export function SystemIcon() {
  return (
    <Stroke>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 2a6 6 0 0 0 0 12Z" fill="currentColor" />
    </Stroke>
  );
}

export function FolderIcon({ className }: { className?: string }) {
  return (
    <Stroke className={className}>
      <path d="M2 4.5h4.2l1.5 1.5H14v7.5H2Z" />
    </Stroke>
  );
}

export function StoryIcon({ className }: { className?: string }) {
  return (
    <Stroke className={className}>
      <path d="M3 3.5h10M3 8h10M3 12.5h6" />
    </Stroke>
  );
}

export function BackIcon() {
  return (
    <Stroke>
      <path d="M9.5 3.5 5 8l4.5 4.5" />
    </Stroke>
  );
}

export function AppearanceIcon() {
  return (
    <Stroke>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 2.5a5.5 5.5 0 0 0 0 11z" fill="currentColor" />
    </Stroke>
  );
}

export function LogIcon() {
  return (
    <Stroke>
      <path d="M5.5 4.5h7M5.5 8h7M5.5 11.5h7" />
      <circle cx="3" cy="4.5" r=".6" fill="currentColor" />
      <circle cx="3" cy="8" r=".6" fill="currentColor" />
      <circle cx="3" cy="11.5" r=".6" fill="currentColor" />
    </Stroke>
  );
}

export function HostsIcon() {
  return (
    <Stroke>
      <rect x="2.5" y="3" width="11" height="4" rx="1.2" />
      <rect x="2.5" y="9" width="11" height="4" rx="1.2" />
      <path d="M5 5h.01M5 11h.01" />
    </Stroke>
  );
}

export function GeneralIcon() {
  return (
    <Stroke>
      <path d="M3 5h6M11 5h2M3 11h2M7 11h6" />
      <circle cx="10" cy="5" r="1.3" />
      <circle cx="6" cy="11" r="1.3" />
    </Stroke>
  );
}

export function ReviewIcon() {
  return (
    <Stroke>
      <path d="M1.8 8S4 3.8 8 3.8 14.2 8 14.2 8 12 12.2 8 12.2 1.8 8 1.8 8z" />
      <circle cx="8" cy="8" r="2" />
    </Stroke>
  );
}

export function AgentsIcon() {
  return (
    <Stroke>
      <circle cx="8" cy="5.5" r="2.5" />
      <path d="M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4" />
    </Stroke>
  );
}

export function CaptureIcon() {
  return (
    <Stroke>
      <path d="M2.5 5.5h2l1-1.5h5l1 1.5h2v7h-11z" />
      <circle cx="8" cy="8.8" r="2.2" />
    </Stroke>
  );
}

export function HoldIcon() {
  return (
    <Stroke>
      <path d="M6 4v8M10 4v8" />
    </Stroke>
  );
}

export function TomlIcon() {
  return (
    <Stroke>
      <path d="M5.5 3C4 3 4 4 4 5v1.2C4 7.2 3.4 8 2.5 8c.9 0 1.5.8 1.5 1.8V11c0 1 0 2 1.5 2M10.5 3c1.5 0 1.5 1 1.5 2v1.2c0 1 .6 1.8 1.5 1.8-.9 0-1.5.8-1.5 1.8V11c0 1 0 2-1.5 2" />
    </Stroke>
  );
}

export function DropIcon() {
  return (
    <Stroke>
      <path d="m4.5 6.5 3.5 3.5 3.5-3.5" />
    </Stroke>
  );
}

export function FoldChevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width={14}
      height={14}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      data-chevron
      className={`size-3.5 shrink-0 text-ink-3 transition-transform duration-150 group-hover:text-ink ${open ? "rotate-90" : ""}`}
    >
      <path d="M6 4l4 4-4 4" />
    </svg>
  );
}

/** A readiness fact: a check when it holds, a warning when it does not. */
export function FactIcon({ ok }: { ok: boolean }) {
  return (
    <Stroke className={ok ? "text-live" : "text-ask-ink"}>
      <circle cx="8" cy="8" r="6.5" />
      <path d={ok ? "M5 8.3 7.1 10.4 11 6.2" : "M8 4.8v3.6M8 11.1v.1"} />
    </Stroke>
  );
}

const CRITERION_MARKS = {
  met: { name: "met", tone: "live", path: "M5 8.3 7.1 10.4 11 6.2" },
  unwitnessed: { name: "not yet witnessed", tone: "wait", path: "M8 4.8V8l2.3 1.6" },
  not_met: { name: "unmet", tone: "need", path: "M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" },
};

export function CriterionMark({ status }: { status: RunCriterion["status"] }) {
  const mark = CRITERION_MARKS[status];
  return (
    <svg
      viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor"
      strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"
      role="img" aria-label={mark.name} data-tone={mark.tone}
      className="size-4 shrink-0" style={{ color: `var(--${mark.tone})` }}
    >
      <circle cx="8" cy="8" r="6.5" />
      <path d={mark.path} />
    </svg>
  );
}

/** The return arrow of a loop back to implement. */
export function LoopIcon() {
  return (
    <Stroke className="text-live">
      <path d="M3.2 8a4.8 4.8 0 1 0 1.4-3.4" />
      <path d="M3.2 2.6v2.6h2.6" />
    </Stroke>
  );
}

export function SettingsIcon() {
  return (
    <Stroke>
      <path d="M2 5h3m4 0h5M2 11h7m4 0h1" />
      <circle cx="7" cy="5" r="2" />
      <circle cx="11" cy="11" r="2" />
    </Stroke>
  );
}

export function TurnArrow({ next = false }: { next?: boolean }) {
  return (
    <Stroke className={next ? "rotate-180" : undefined}>
      <path d="M13 8H3m5-5L3 8l5 5" />
    </Stroke>
  );
}

export function TranscriptIcon({ kind }: { kind: "prompt" | "note" | "said" | "tool" | "error" | "end" }) {
  const paths = {
    prompt: "M3.5 1.5h6l3 3v10h-9ZM9.5 1.5v3h3M6 8h4M6 11h4",
    note: "M3.5 1.5h6l3 3v10h-9ZM9.5 1.5v3h3M6 8h4M6 11h4",
    said: "M2 2.5h12v9H6l-4 3v-12Z",
    tool: "m3 4 4 4-4 4M9 12h4",
    error: "M8 1.5a6.5 6.5 0 1 1 0 13a6.5 6.5 0 1 1 0-13zM5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4",
    end: "M2 8h12M8 4v8",
  };
  return <Stroke><path d={paths[kind]} /></Stroke>;
}

export function MergeQueueIcon() {
  return (
    <Stroke>
      <path d="M2 4h7M2 8h5M2 12h7M12.5 13V3M10 5.5 12.5 3 15 5.5" />
    </Stroke>
  );
}

export function FlowIcon() {
  return (
    <Stroke>
      <path d="M2 13.5h12M3.5 11V8M6.5 11V4M9.5 11V6.5M12.5 11V9" />
    </Stroke>
  );
}
