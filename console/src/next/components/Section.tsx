import { useId, useState, type ReactNode } from "react";
import { FoldChevron } from "./icons";

export function Section({
  label,
  count,
  hot = false,
  summary,
  defaultOpen = true,
  onToggle,
  children,
}: {
  label: string;
  count?: number;
  hot?: boolean;
  summary?: ReactNode;
  defaultOpen?: boolean;
  onToggle?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const body = useId();
  const toggle = () => {
    setOpen(!open);
    onToggle?.(!open);
  };
  return (
    <section aria-label={label} className="flex flex-col gap-3.5">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h2 className="flex items-center gap-2.5">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={body}
            onClick={toggle}
            className="group -ml-1.5 inline-flex min-h-10 items-center gap-2.5 pl-1.5 font-sans text-[19px] font-bold tracking-[-0.005em] text-ink"
          >
            <FoldChevron open={open} />
            {label}
          </button>
          {count != null && (
            <span className={`font-mono text-[13px] font-medium leading-none ${hot && count > 0 ? "text-need" : "text-ink-3"}`}>{count}</span>
          )}
        </h2>
        {summary != null && <span className="truncate font-mono text-[12.5px] leading-[1.4] text-ink-3">{summary}</span>}
      </header>
      <div id={body} hidden={!open}>
        {open && children}
      </div>
    </section>
  );
}
