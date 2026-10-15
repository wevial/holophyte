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

/** The flat five-petal mark, filled with the current colour. */
export function Mark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={22} height={22} fill="currentColor" aria-hidden="true" className={className}>
      <path d="M12 2.5c1.6 1.8 3.1 4.4 3.1 7.2 0 2.7-1.4 5.3-3.1 7.6-1.7-2.3-3.1-4.9-3.1-7.6 0-2.8 1.5-5.4 3.1-7.2Z" />
      <path d="M11.6 17.6C9 15.4 5.6 13.9 2.6 14.3c1.6 2.4 5 4 9 3.3Z" />
      <path d="M12.4 17.6c2.6-2.2 6-3.7 9-3.3-1.6 2.4-5 4-9 3.3Z" />
      <path d="M11.6 16.8C8.7 14.6 6.4 11.3 6 8c2.7 1 5 4.6 5.6 8.8Z" opacity="0.7" />
      <path d="M12.4 16.8c2.9-2.2 5.2-5.5 5.6-8.8-2.7 1-5 4.6-5.6 8.8Z" opacity="0.7" />
    </svg>
  );
}

export function OverviewIcon() {
  return (
    <Stroke>
      <rect x="2" y="2" width="5" height="5" />
      <rect x="9" y="2" width="5" height="5" />
      <rect x="2" y="9" width="5" height="5" />
      <rect x="9" y="9" width="5" height="5" />
    </Stroke>
  );
}

export function RunsIcon() {
  return (
    <Stroke>
      <path d="M2 8h3l2-5 2 10 2-5h3" />
    </Stroke>
  );
}

export function TicketsIcon() {
  return (
    <Stroke>
      <rect x="2.5" y="2" width="11" height="12" />
      <path d="M5 5.5h6M5 8h6M5 10.5h3.5" />
    </Stroke>
  );
}

export function FindingsIcon() {
  return (
    <Stroke>
      <circle cx="7" cy="7" r="4.2" />
      <path d="m10.2 10.2 3.6 3.6" />
    </Stroke>
  );
}

export function ChevronIcon({ folded }: { folded: boolean }) {
  return (
    <Stroke>
      <path d={folded ? "m6 3.5 4.5 4.5L6 12.5" : "M10 3.5 5.5 8l4.5 4.5"} />
    </Stroke>
  );
}

export function SignalIcon({ className }: { className?: string }) {
  return (
    <Stroke className={className}>
      <path d="M2 13.5h1.5M6 13.5V10M9.5 13.5V6.5M13 13.5V3" />
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
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" />
    </Stroke>
  );
}
