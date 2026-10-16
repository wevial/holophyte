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

/** The flat five-petal mark over its ground line. */
export function Mark() {
  return (
    <svg viewBox="0 0 24 24" width={24} height={24} aria-hidden="true" className="shrink-0">
      <g className="fill-act stroke-canvas" strokeWidth={1.1} strokeLinejoin="round">
        <path d="M12 17C7.4 17.6 3.6 15.6 2 12.2 5.6 11.8 9.4 13.6 12 17Z" />
        <path d="M12 17C16.6 17.6 20.4 15.6 22 12.2 18.4 11.8 14.6 13.6 12 17Z" />
        <path d="M12 17C8.2 16.6 5.6 13 6 8 9 9.2 11.2 12.6 12 17Z" />
        <path d="M12 17C15.8 16.6 18.4 13 18 8 15 9.2 12.8 12.6 12 17Z" />
        <path d="M12 4C15.2 7.2 15.2 13 12 17 8.8 13 8.8 7.2 12 4Z" />
      </g>
      <path d="M6 20.2H18" fill="none" className="stroke-ink-3" strokeWidth={1.3} strokeLinecap="round" />
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

export function ClassicIcon() {
  return (
    <Stroke>
      <path d="M6.5 3H3v10h10V9.5" />
      <path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5" />
    </Stroke>
  );
}

export function SignalIcon({ className }: { className?: string }) {
  return (
    <Stroke className={className}>
      <path d="M2.8 7a7.4 7.4 0 0 1 10.4 0M5 9.4a4.2 4.2 0 0 1 6 0M8 12.2v.1" />
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
    <Stroke className={ok ? "text-live" : "text-ask"}>
      <circle cx="8" cy="8" r="6.5" />
      <path d={ok ? "M5 8.3 7.1 10.4 11 6.2" : "M8 4.8v3.6M8 11.1v.1"} />
    </Stroke>
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
