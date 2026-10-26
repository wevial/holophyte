import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { dayStart } from "../history";

const SEARCH_DEBOUNCE_MS = 200;

type PageSearch = Record<string, unknown>;

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** A list page's search: its debounced, trimmed text is the address's
 *  `q`, and `/` anywhere on the page focuses it. */
export function SearchField({ label, width = "w-56" }: { label: string; width?: string }) {
  const { q } = useSearch({ strict: false }) as { q?: string };
  const navigate = useNavigate();
  const [text, setText] = useState(q ?? "");
  const written = useRef(q);
  const field = useRef<HTMLInputElement>(null);
  const write = (next: string | undefined) => {
    written.current = next;
    void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, page: undefined, q: next }), replace: true });
  };
  useEffect(() => {
    if (q === written.current) return;
    written.current = q;
    setText(q ?? "");
  }, [q]);
  useEffect(() => {
    const next = text.trim() === "" ? undefined : text.trim();
    if (next === written.current) return;
    const timer = setTimeout(() => write(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text]);
  useEffect(() => {
    const focus = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || typing(event.target)) return;
      event.preventDefault();
      field.current?.focus();
    };
    document.addEventListener("keydown", focus);
    return () => document.removeEventListener("keydown", focus);
  }, []);
  return (
    <input
      ref={field}
      type="search"
      aria-label={label}
      placeholder="Search"
      value={text}
      onChange={(event) => setText(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        setText("");
        write(undefined);
      }}
      className={`min-h-8 ${width} border border-rule bg-paper px-3 font-sans text-[12.5px] text-ink placeholder:text-ink-3 focus:outline-2 focus:-outline-offset-1 focus:outline-focus`}
    />
  );
}

interface FilterOption {
  value: string;
  label: string;
}

/** `all` under `allLabel`, then each of `options`, as the address's `param`;
 *  `all` drops it. */
export function FilterSelect({ label, param, allLabel, options }: { label: string; param: string; allLabel: string; options: readonly FilterOption[] }) {
  const search = useSearch({ strict: false }) as PageSearch;
  const navigate = useNavigate();
  const value = search[param];
  return (
    <select
      aria-label={label}
      value={typeof value === "string" ? value : "all"}
      onChange={(event) => {
        const picked = options.find((each) => each.value === event.target.value)?.value;
        void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, [param]: picked }) });
      }}
      className="min-h-8 cursor-pointer border border-rule bg-paper px-2.5 font-sans text-[12.5px] text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
    >
      <option value="all">{allLabel}</option>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

const SEGMENT = "min-h-9 cursor-pointer px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.1em] whitespace-nowrap text-ink-2 not-first:border-l not-first:border-ink-3 hover:bg-paper-2 aria-pressed:bg-ink aria-pressed:text-paper";

/** A popover's open state, closed by `hidden`, by Escape or by a press
 *  outside `box`, which gives focus back to `button` when it was inside. */
function usePopover(hidden: boolean) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = () => {
      const within = box.current?.contains(document.activeElement) ?? false;
      setOpen(false);
      if (within || document.activeElement === document.body) button.current?.focus();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      dismiss();
    };
    const onPress = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) dismiss();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPress);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPress);
    };
  }, [open]);
  useEffect(() => {
    if (hidden) setOpen(false);
  }, [hidden]);
  return { open, setOpen, box, button };
}

function checkedOf(search: PageSearch, param: string): string[] {
  const raw = search[param];
  return typeof raw === "string" ? raw.split(",") : [];
}

function useWrite() {
  const navigate = useNavigate();
  return (change: PageSearch) => void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, page: undefined, ...change }) });
}

function FilterIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 14 14" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.4} strokeLinecap="round">
      <path d="M1.5 3h11M3.5 7h7M5.5 11h3" />
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 14 14" width={13} height={13} fill="none" stroke="currentColor" strokeWidth={1.3}>
      <rect x="1.5" y="2.5" width="11" height="10" />
      <path d="M1.5 5.5h11M4.5 1v3M9.5 1v3" />
    </svg>
  );
}

export interface FilterSection {
  label: string;
  param: string;
  options: readonly (FilterOption & { count: number })[];
}

function Check({ name, checked, mixed = false, onChange, children, head = false, disabled = false }: {
  name: string; checked: boolean; mixed?: boolean; onChange: () => void; children: ReactNode; head?: boolean; disabled?: boolean;
}) {
  return (
    <label className={`flex min-h-8 cursor-pointer items-center gap-3 ${head ? "font-semibold text-ink" : "pl-7 text-ink-2"}`}>
      <input type="checkbox" aria-label={name} checked={checked} disabled={disabled} onChange={onChange}
        ref={(input) => { if (input) input.indeterminate = mixed; }}
        className="size-3.5 shrink-0 cursor-pointer accent-act" />
      {children}
    </label>
  );
}

function Section({ section, search, write }: { section: FilterSection; search: PageSearch; write: (change: PageSearch) => void }) {
  const id = useId();
  const checked = checkedOf(search, section.param);
  const on = section.options.filter((option) => checked.includes(option.value));
  const all = section.options.length > 0 && on.length === section.options.length;
  const set = (values: string[]) => {
    const kept = section.options.map((option) => option.value).filter((value) => values.includes(value));
    write({ [section.param]: kept.length > 0 ? kept.join(",") : undefined });
  };
  return (
    <fieldset aria-labelledby={id} className="m-0 min-w-0 border-rule px-4 py-2.5 not-first:border-t">
      <legend className="float-left w-full">
        <Check head name={section.label} checked={all} mixed={on.length > 0 && !all} disabled={section.options.length === 0}
          onChange={() => set(all ? [] : section.options.map((option) => option.value))}>
          <span id={id}>{section.label}</span>{" "}
          {on.length > 0 && <span className="ml-auto font-mono text-[12px] font-normal text-ink-3">{on.length} of {section.options.length}</span>}
        </Check>
      </legend>
      {section.options.map((option) => (
        <Check key={option.value} name={option.label} checked={checked.includes(option.value)}
          onChange={() => set(checked.includes(option.value) ? checked.filter((value) => value !== option.value) : [...checked, option.value])}>
          <span className="min-w-0 [overflow-wrap:anywhere]">{option.label}</span>{" "}
          <span className="ml-auto font-mono text-[12px] tabular-nums text-act">{option.count}</span>
        </Check>
      ))}
    </fieldset>
  );
}

/** A Filter button over a dialog of checkbox `sections`, each section's
 *  checked values written to its own search param as a comma list in the
 *  section's order; `hidden` closes it. */
export function FilterPopover({ label, sections, hidden }: { label: string; sections: readonly FilterSection[]; hidden: boolean }) {
  const search = useSearch({ strict: false }) as PageSearch;
  const write = useWrite();
  const { open, setOpen, box, button } = usePopover(hidden);
  const count = sections.reduce((sum, section) => sum + section.options.filter((option) => checkedOf(search, section.param).includes(option.value)).length, 0);
  return (
    <div ref={box} className="relative shrink-0">
      <button
        ref={button}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className={`inline-flex min-h-9 cursor-pointer items-center gap-2 whitespace-nowrap border px-2 font-sans text-[14px] hover:bg-paper-2 ${count > 0 ? "border-act text-act" : "border-transparent text-ink"}`}
      >
        <FilterIcon />
        {count > 0 ? `Filter · ${count}` : "Filter"}
      </button>
      {open && (
        <div role="dialog" aria-label={label}
          className="absolute top-full left-0 z-20 mt-1 max-h-[calc(100vh-11rem)] w-80 overflow-y-auto border border-rule bg-paper text-[14px] shadow-[0_8px_24px_rgb(0_0_0/0.12)]">
          {sections.map((section) => <Section key={section.param} section={section} search={search} write={write} />)}
        </div>
      )}
    </div>
  );
}

const DAY_LABEL = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

function dayLabel(day: string): string {
  return DAY_LABEL.format(dayStart(day));
}

/** `from` and `to`, local `YYYY-MM-DD` days, as "Oct 1 – Oct 20". */
export function dayRangeLabel(from: string | undefined, to: string | undefined): string {
  if (from != null && to != null) return `${dayLabel(from)} – ${dayLabel(to)}`;
  return from != null ? `From ${dayLabel(from)}` : to != null ? `To ${dayLabel(to)}` : "";
}

/** A group of `presets` written as the search's `range`, `fallback`
 *  dropping it, and a Custom range popover writing `from` and `to`, which
 *  win over `range` while set; `hidden` closes it. */
export function RangeButtons({ label, presets, fallback, hidden }: {
  label: string; presets: readonly FilterOption[]; fallback: string; hidden: boolean;
}) {
  const { range, from, to } = useSearch({ strict: false }) as { range?: string; from?: string; to?: string };
  const write = useWrite();
  const { open, setOpen, box, button } = usePopover(hidden);
  const custom = from != null || to != null;
  const field = "min-h-8 border border-rule bg-paper px-2 font-mono text-[12.5px] text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus";
  return (
    <div ref={box} className="relative shrink-0">
      <div role="group" aria-label={label} className="inline-flex border border-ink-3">
        {presets.map((preset) => (
          <button key={preset.value} type="button" aria-pressed={!custom && (range ?? fallback) === preset.value}
            onClick={() => write({ range: preset.value === fallback ? undefined : preset.value, from: undefined, to: undefined })} className={SEGMENT}>
            {preset.label}
          </button>
        ))}
        <button ref={button} type="button" aria-label="Custom range" aria-pressed={custom} aria-haspopup="true" aria-expanded={open}
          onClick={() => setOpen(!open)} className={`${SEGMENT} inline-flex items-center gap-2 normal-case tracking-normal`}>
          <CalendarIcon />
          {custom && dayRangeLabel(from, to)}
        </button>
      </div>
      {open && (
        <div role="group" aria-label={`${label} range`}
          className="absolute top-full right-0 z-20 mt-1 flex flex-col gap-3 border border-rule bg-paper p-4 font-sans text-[13px] text-ink-2 shadow-[0_8px_24px_rgb(0_0_0/0.12)]">
          <label className="flex items-center justify-between gap-3">
            From
            <input type="date" value={from ?? ""} max={to} className={field}
              onChange={(event) => write({ range: undefined, from: event.target.value || undefined })} />
          </label>
          <label className="flex items-center justify-between gap-3">
            To
            <input type="date" value={to ?? ""} min={from} className={field}
              onChange={(event) => write({ range: undefined, to: event.target.value || undefined })} />
          </label>
          <button type="button" onClick={() => { setOpen(false); button.current?.focus(); }}
            className="min-h-8 cursor-pointer self-end border border-ink-3 px-3 font-mono text-[11px] font-medium uppercase tracking-[0.1em] text-ink hover:bg-paper-2">
            Done
          </button>
        </div>
      )}
    </div>
  );
}
