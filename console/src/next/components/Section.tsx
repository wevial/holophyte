import { useId, useState, type ReactNode } from "react";

export function Section({
  label,
  count,
  summary,
  defaultOpen = true,
  onToggle,
  children,
}: {
  label: string;
  count?: number;
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
    <section aria-label={label} className="flex flex-col gap-3">
      <header className="flex items-baseline gap-3 border-b border-rule-2 pb-2">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={body}
          onClick={toggle}
          className="font-sans text-[12px] font-semibold uppercase tracking-[0.08em] text-ink-3 transition-colors duration-(--motion-colour) hover:text-ink"
        >
          {label}
        </button>
        {count != null && <span className="font-mono text-[12px] text-ink-2">{count}</span>}
        {summary != null && <span className="truncate font-sans text-[13px] text-ink-3">{summary}</span>}
      </header>
      <div id={body} hidden={!open}>
        {open && children}
      </div>
    </section>
  );
}
