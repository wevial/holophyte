import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { dayStart } from "../history";
import { Btn } from "./Btn";
import { Picker, type PickerOption as FilterOption } from "./Picker";
import { Segmented } from "./Segmented";

const SEARCH_DEBOUNCE_MS = 200;

type PageSearch = Record<string, unknown>;

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** A list page's search: its debounced, trimmed text is the address's
 *  `q`, and `/` anywhere on the page focuses it. */
export function SearchField({ label, width = "w-56", className, autoFocus, onBlur }: {
  label: string; width?: string; className?: string; autoFocus?: boolean; onBlur?: (text: string) => void;
}) {
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
  const input = (
    <input
      ref={field}
      type="search"
      aria-label={label}
      placeholder="Search"
      value={text}
      autoFocus={autoFocus}
      onBlur={(event) => onBlur?.(event.currentTarget.value)}
      onChange={(event) => setText(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        setText("");
        write(undefined);
      }}
      className={className ?? FILLED_INPUT}
    />
  );
  if (className != null) return input;
  return (
    <label className={`flex h-[30px] ${width} shrink-0 items-center gap-2 rounded-button bg-selected px-2.5 text-ink-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus`}>
      <SearchIcon />
      {input}
    </label>
  );
}

const FILLED_INPUT = "h-full min-w-0 flex-1 border-0 bg-transparent font-sans text-[14px] text-ink outline-none! placeholder:text-ink-3 [&::-webkit-search-cancel-button]:hidden";

const TOOL = "relative inline-flex h-[28px] min-w-7 shrink-0 cursor-pointer items-center justify-center gap-[5px] rounded-button px-1.5 text-ink-2 transition-colors hover:bg-hover hover:text-ink aria-expanded:bg-selected aria-expanded:text-ink";

/** A Search button that becomes a `SearchField` with focus when pressed or
 *  on `/`, shown already while the address holds `q`, and a button again
 *  when it loses focus empty. */
export function SearchToggle({ label }: { label: string }) {
  const { q } = useSearch({ strict: false }) as { q?: string };
  const [opened, setOpened] = useState(false);
  const shown = opened || q != null;
  const held = useRef(false);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!shown && held.current) button.current?.focus();
    held.current = false;
  }, [shown]);
  useEffect(() => {
    if (shown) return;
    const open = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || typing(event.target)) return;
      event.preventDefault();
      setOpened(true);
    };
    document.addEventListener("keydown", open);
    return () => document.removeEventListener("keydown", open);
  }, [shown]);
  if (!shown) {
    return (
      <button ref={button} type="button" aria-label={label} title="Search" onClick={() => setOpened(true)} className={TOOL}>
        <SearchIcon />
      </button>
    );
  }
  return (
    <label onFocus={() => (held.current = true)} className="flex h-[28px] w-60 min-w-28 shrink items-center gap-2 rounded-button bg-selected px-2.5 text-ink-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus">
      <SearchIcon />
      <SearchField label={label} autoFocus={opened} onBlur={(text) => {
        held.current = false;
        if (text.trim() === "") setOpened(false);
      }}
        className={FILLED_INPUT} />
    </label>
  );
}

/** `all` under `allLabel`, then each of `options`, as the address's `param`;
 *  `all` drops it. */
export function FilterSelect({ label, param, allLabel, options }: { label: string; param: string; allLabel: string; options: readonly FilterOption[] }) {
  const search = useSearch({ strict: false }) as PageSearch;
  const navigate = useNavigate();
  const value = search[param];
  return (
    <Picker
      label={label}
      options={[{ value: "all", label: allLabel }, ...options]}
      value={typeof value === "string" ? value : "all"}
      onPick={(next) => {
        const picked = options.find((each) => each.value === next)?.value;
        void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, [param]: picked }) });
      }}
    />
  );
}

const DATE_FIELD = "h-[28px] rounded-button border-0 bg-selected px-2 font-mono text-[12px] text-ink [color-scheme:inherit] focus:outline-2 focus:outline-offset-1 focus:outline-focus";

/** A popover's open state, closed by `hidden`, Escape or a press outside
 *  `box`, which gives focus back to `button` and focuses nothing else. */
export function usePopover(hidden: boolean) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    button.current?.focus();
  };
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      close();
    };
    const onPress = (event: PointerEvent) => {
      if (box.current?.contains(event.target as Node)) return;
      event.preventDefault();
      close();
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
  return { open, setOpen, close, box, button };
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

function SearchIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" className="size-4 shrink-0">
      <circle cx="7" cy="7" r="4" />
      <path d="m10 10 3 3" />
    </svg>
  );
}

function ToolFilterIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" className="size-4 shrink-0">
      <path d="M2.5 4h11M4.5 8h7M6.5 12h3" />
    </svg>
  );
}

function ToolCalendarIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" className="size-4 shrink-0">
      <rect x="2.5" y="3.5" width="11" height="10" rx="2" />
      <path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" />
    </svg>
  );
}

function CheckMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="size-3.5 shrink-0 text-ink">
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
  );
}

export interface FilterSection {
  label: string;
  param: string;
  options: readonly (FilterOption & { count: number; colour?: string })[];
  mono?: boolean;
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

export interface SortSection {
  keys: readonly FilterOption[];
  orders: readonly FilterOption[];
  sort: string;
  order: string;
  onSort: (key: string) => void;
  onOrder: (order: string) => void;
}

function Radio({ name, label, checked, onPick }: { name: string; label: string; checked: boolean; onPick: () => void }) {
  return (
    <label className="flex min-h-8 cursor-pointer items-center gap-3 pl-7 text-ink-2">
      <input type="radio" name={name} checked={checked} onChange={onPick} className="size-3.5 shrink-0 cursor-pointer accent-act" />
      {label}
    </label>
  );
}

function Sorting({ sorting }: { sorting: SortSection }) {
  const id = useId();
  return (
    <fieldset aria-labelledby={id} className="m-0 min-w-0 border-rule px-4 py-2.5">
      <legend id={id} className="float-left flex min-h-8 w-full items-center font-semibold text-ink">Sort</legend>
      {sorting.keys.map((key) => <Radio key={key.value} name={`${id}-key`} label={key.label} checked={sorting.sort === key.value} onPick={() => sorting.onSort(key.value)} />)}
      <div role="radiogroup" aria-label="Order" className="mt-1 border-t border-rule-2 pt-1">
        {sorting.orders.map((order) => <Radio key={order.value} name={`${id}-order`} label={order.label} checked={sorting.order === order.value} onPick={() => sorting.onOrder(order.value)} />)}
      </div>
    </fieldset>
  );
}

const MENU_HEAD = "flex h-[26px] items-center gap-1.5 px-2 text-[12px] text-ink-3";
const MENU_ROW = "flex h-[26px] cursor-pointer items-center gap-2 rounded-button px-2 text-ink-2 transition-colors hover:bg-hover hover:text-ink has-checked:font-medium has-checked:text-ink has-focus-visible:outline-2 has-focus-visible:outline-focus";

function MenuCheck({ type, name, checked, onChange, children, className = MENU_ROW }: {
  type: "checkbox" | "radio"; name: string; checked: boolean; onChange: () => void; children: ReactNode; className?: string;
}) {
  return (
    <label className={className}>
      <input type={type} name={type === "radio" ? name : undefined} aria-label={type === "checkbox" ? name : undefined} checked={checked} onChange={onChange} className="sr-only" />
      <span className={`inline-flex w-3.5 shrink-0 items-center justify-center ${checked ? "" : "invisible"}`}><CheckMark /></span>
      {children}
    </label>
  );
}

function MenuSection({ section, search, write }: { section: FilterSection; search: PageSearch; write: (change: PageSearch) => void }) {
  const id = useId();
  const checked = checkedOf(search, section.param);
  const set = (values: string[]) => {
    const kept = section.options.map((option) => option.value).filter((value) => values.includes(value));
    write({ [section.param]: kept.length > 0 ? kept.join(",") : undefined });
  };
  return (
    <fieldset aria-labelledby={id} className="m-0 min-w-0 border-0 p-0">
      <legend id={id} className={`${MENU_HEAD} float-left w-full`}>{section.label}</legend>
      {section.options.map((option) => (
        <MenuCheck key={option.value} type="checkbox" name={option.label} checked={checked.includes(option.value)}
          onChange={() => set(checked.includes(option.value) ? checked.filter((value) => value !== option.value) : [...checked, option.value])}
          className={`${MENU_ROW} ${option.count === 0 ? "text-ink-3" : ""}`}>
          {option.colour && <i aria-hidden="true" className="size-[7px] shrink-0 rounded-full" style={{ background: option.colour }} />}
          <span className={`min-w-0 truncate ${section.mono ? "font-mono text-[12px]" : ""}`}>{option.label}</span>{" "}
          <span className="ml-auto font-mono text-[12px] font-normal tabular-nums text-ink-3">{option.count}</span>
        </MenuCheck>
      ))}
    </fieldset>
  );
}

function OrderArrow({ down }: { down: boolean }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" className="size-3.5">
      <path d={down ? "M8 3v10M4.5 9.5 8 13l3.5-3.5" : "M8 13V3M4.5 6.5 8 3l3.5 3.5"} />
    </svg>
  );
}

function MenuSorting({ sorting }: { sorting: SortSection }) {
  const id = useId();
  return (
    <fieldset aria-labelledby={id} className="m-0 min-w-0 border-0 p-0">
      <legend className={`${MENU_HEAD} float-left w-full`}>
        <span id={id}>Sort</span>
        <span className="ml-auto inline-flex gap-0.5">
          {sorting.orders.map((order) => (
            <button key={order.value} type="button" aria-pressed={sorting.order === order.value} aria-label={order.label} title={order.label} onClick={() => sorting.onOrder(order.value)}
              className="inline-grid size-[22px] cursor-pointer place-items-center rounded-[5px] text-ink-3 hover:bg-hover hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink">
              <OrderArrow down={order.value === "desc"} />
            </button>
          ))}
        </span>
      </legend>
      {sorting.keys.map((key) => (
        <MenuCheck key={key.value} type="radio" name={`${id}-key`} checked={sorting.sort === key.value} onChange={() => sorting.onSort(key.value)}>{key.label}</MenuCheck>
      ))}
    </fieldset>
  );
}

/** A Filter button over a dialog of checkbox `sections`, each section's
 *  checked values written to its own search param as a comma list in the
 *  section's order, after an optional `sorting` and `extra`; `hidden`
 *  closes it. An `icon` trigger is named `label` and the count of filters
 *  on, with a dot while any is. */
export function FilterPopover({ label, sections, hidden, sorting, extra, icon = false }: {
  label: string; sections: readonly FilterSection[]; hidden: boolean; sorting?: SortSection; extra?: ReactNode; icon?: boolean;
}) {
  const search = useSearch({ strict: false }) as PageSearch;
  const write = useWrite();
  const { open, setOpen, box, button } = usePopover(hidden);
  const count = sections.reduce((sum, section) => sum + section.options.filter((option) => checkedOf(search, section.param).includes(option.value)).length, 0);
  const on = count === 1 ? "1 filter on" : `${count} filters on`;
  return (
    <div ref={box} className={icon ? "shrink-0" : "relative shrink-0"}>
      {icon ? (
        <button ref={button} type="button" aria-haspopup="dialog" aria-expanded={open} aria-label={count > 0 ? `${label}, ${on}` : label}
          title={label} onClick={() => setOpen(!open)} className={TOOL}>
          <ToolFilterIcon />
          {count > 0 && <i data-filter-dot aria-hidden="true" className="absolute top-1 right-1 size-1.5 rounded-full bg-accent shadow-[0_0_0_2px_var(--canvas)]" />}
        </button>
      ) : (
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
      )}
      {open && icon && (
        <div role="dialog" aria-label={label}
          className="absolute top-full right-0 z-20 mt-1.5 grid max-h-[min(560px,calc(100vh-11rem))] w-[min(600px,calc(100vw-2rem))] grid-cols-3 gap-x-2.5 overflow-y-auto rounded-[9px] border border-hair-2 bg-raised p-2 text-[13px] text-ink-2 shadow-card">
          <div>
            {sorting && <MenuSorting sorting={sorting} />}
            {extra}
          </div>
          <div className="col-span-2 grid grid-cols-2 grid-rows-[auto_1fr] items-start gap-x-2.5 gap-y-2 [&>fieldset:nth-child(2)]:col-start-2 [&>fieldset:nth-child(2)]:row-span-2">
            {sections.map((section) => <MenuSection key={section.param} section={section} search={search} write={write} />)}
          </div>
          <div className="col-span-3 mt-1.5 flex justify-end border-t border-hair pt-1.5">
            <button type="button" disabled={count === 0} onClick={() => write(Object.fromEntries(sections.map((section) => [section.param, undefined])))}
              className="h-[26px] cursor-pointer rounded-button px-2 text-[12px] font-medium text-ink-2 hover:bg-hover hover:text-ink disabled:cursor-default disabled:text-ink-3 disabled:hover:bg-transparent">
              Clear
            </button>
          </div>
        </div>
      )}
      {open && !icon && (
        <div role="dialog" aria-label={label}
          className="absolute top-full left-0 z-20 mt-1 max-h-[min(560px,calc(100vh-11rem))] w-80 overflow-y-auto border border-rule bg-paper text-[14px] shadow-[0_8px_24px_rgb(0_0_0/0.12)]">
          {sorting && <Sorting sorting={sorting} />}
          {sections.map((section) => <Section key={section.param} section={section} search={search} write={write} />)}
        </div>
      )}
    </div>
  );
}

/** A labelled group of pressed buttons, one per `options` value, the
 *  pressed one `value`. */
export function PressedGroup<V extends string | number>({ label, options, value, onPick }: {
  label: string; options: readonly V[]; value: V; onPick: (picked: V) => void;
}) {
  const id = useId();
  return (
    <div className="mt-2">
      <div id={id} className={MENU_HEAD}>{label}</div>
      <div role="group" aria-labelledby={id} className="mx-2 mt-0.5 inline-flex rounded-[7px] bg-canvas p-0.5">
        {options.map((option) => (
          <button key={option} type="button" aria-pressed={option === value} onClick={() => onPick(option)}
            className="h-6 cursor-pointer rounded-[5px] px-2.5 font-mono text-[12px] font-medium text-ink-3 hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink">
            {option}
          </button>
        ))}
      </div>
    </div>
  );
}

const DAY_LABEL = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

export function dayLabel(day: string): string {
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
  const { open, setOpen, close, box, button } = usePopover(hidden);
  const custom = from != null || to != null;
  const fromId = useId();
  const toId = useId();
  return (
    <div ref={box} className="relative flex shrink-0 items-center gap-1.5">
      <Segmented label={label} options={presets} value={custom ? undefined : (range ?? fallback)}
        onPick={(picked) => write({ range: picked === fallback ? undefined : picked, from: undefined, to: undefined })} />
      <button ref={button} type="button" aria-label="Custom range" title="Custom range" aria-pressed={custom} aria-haspopup="true" aria-expanded={open}
        onClick={() => setOpen(!open)} className={`${TOOL} aria-pressed:bg-selected aria-pressed:text-ink`}>
        <ToolCalendarIcon />
        {custom && <span className="font-mono text-[12px] tabular-nums whitespace-nowrap text-accent">{dayRangeLabel(from, to)}</span>}
      </button>
      {open && (
        <div role="group" aria-label={`${label} range`}
          className="absolute top-full left-0 z-20 mt-1.5 flex w-62 flex-col gap-2 rounded-[9px] border border-hair-2 bg-raised p-2 text-[13px] text-ink-2 shadow-card">
          <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1.5 px-2 pt-0.5">
            <label htmlFor={fromId} className="text-[12px] text-ink-3">From</label>
            <input id={fromId} type="date" value={from ?? ""} max={to} className={DATE_FIELD}
              onChange={(event) => write({ range: undefined, from: event.target.value || undefined })} />
            <label htmlFor={toId} className="text-[12px] text-ink-3">To</label>
            <input id={toId} type="date" value={to ?? ""} min={from} className={DATE_FIELD}
              onChange={(event) => write({ range: undefined, to: event.target.value || undefined })} />
          </div>
          <Btn onClick={close} className="self-end">Done</Btn>
        </div>
      )}
    </div>
  );
}

export interface RangePreset extends FilterOption {
  name: string;
  back: number | null;
}

function dayBefore(now: number, days: number): string {
  const at = new Date(now);
  const day = new Date(at.getFullYear(), at.getMonth(), at.getDate() - days);
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

/** A calendar button labelled with the picked range over a Range dialog of
 *  `presets`, picking one writes `range` (`fallback` dropping it) and
 *  closes it, and From and To dates, showing the picked preset's days up to
 *  `now`'s, write both `from` and `to`, which win over `range` while set;
 *  `hidden` closes it. */
export function RangePopover({ presets, fallback, hidden, now }: { presets: readonly RangePreset[]; fallback: string; hidden: boolean; now: number }) {
  const { range, from, to } = useSearch({ strict: false }) as { range?: string; from?: string; to?: string };
  const write = useWrite();
  const { open, setOpen, close, box, button } = usePopover(hidden);
  const custom = from != null || to != null;
  const picked = custom ? undefined : (presets.find((preset) => preset.value === (range ?? fallback)) ?? presets.find((preset) => preset.value === fallback));
  const shownFrom = custom ? from : picked?.back != null ? dayBefore(now, picked.back) : undefined;
  const shownTo = custom ? to : dayBefore(now, 0);
  const fromId = useId();
  const toId = useId();
  return (
    <div ref={box} className="shrink-0">
      <button ref={button} type="button" aria-haspopup="dialog" aria-expanded={open} aria-label={`Range: ${picked?.name ?? dayRangeLabel(from, to)}`}
        title={picked?.name ?? "Custom range"} onClick={() => setOpen(!open)} className={TOOL}>
        <ToolCalendarIcon />
        <span className="font-mono text-[12px] tabular-nums whitespace-nowrap text-accent">{picked?.label ?? dayRangeLabel(from, to)}</span>
      </button>
      {open && (
        <div role="dialog" aria-label="Range"
          className="absolute top-full right-0 z-20 mt-1.5 w-62 rounded-[9px] border border-hair-2 bg-raised p-2 text-[13px] text-ink-2 shadow-card">
          <div role="radiogroup" aria-label="Started">
            {presets.map((preset) => (
              <button key={preset.value} type="button" role="radio" aria-checked={preset === picked}
                onClick={() => {
                  write({ range: preset.value === fallback ? undefined : preset.value, from: undefined, to: undefined });
                  close();
                }}
                className="flex h-[26px] w-full cursor-pointer items-center gap-2 rounded-button px-2 text-left text-ink-2 hover:bg-hover hover:text-ink aria-checked:font-medium aria-checked:text-ink">
                <span className={`inline-flex ${preset === picked ? "" : "invisible"}`}><CheckMark /></span>
                {preset.name}
              </button>
            ))}
          </div>
          <div className="my-1.5 h-px bg-hair" />
          <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1.5 px-2 pt-0.5 pb-1">
            <label htmlFor={fromId} className="text-[12px] text-ink-3">From</label>
            <input id={fromId} type="date" value={shownFrom ?? ""} max={shownTo} className={DATE_FIELD}
              onChange={(event) => write({ range: undefined, from: event.target.value || undefined, to: shownTo })} />
            <label htmlFor={toId} className="text-[12px] text-ink-3">To</label>
            <input id={toId} type="date" value={shownTo ?? ""} min={shownFrom} className={DATE_FIELD}
              onChange={(event) => write({ range: undefined, from: shownFrom, to: event.target.value || undefined })} />
          </div>
        </div>
      )}
    </div>
  );
}

export interface FilterChip<C> {
  word: string;
  value: string;
  clears: C;
}

export function FilterChips<C>({ chips, onClear, onClearAll }: { chips: FilterChip<C>[]; onClear: (clears: C) => void; onClearAll: () => void }) {
  return (
    <div data-chips className="flex flex-wrap items-center gap-2">
      {chips.map((chip) => (
        <span key={chip.word} className="inline-flex min-h-[28px] items-center gap-2 border border-rule bg-paper pr-1 pl-2.5 font-mono text-[12px] text-ink-2">
          <span>
            {chip.word}: <b className="font-semibold text-ink">{chip.value}</b>
          </span>
          <button type="button" aria-label={`Remove ${chip.word}: ${chip.value}`} onClick={() => onClear(chip.clears)}
            className="inline-flex size-6 cursor-pointer items-center justify-center text-ink-3 hover:text-ink">
            <svg aria-hidden="true" viewBox="0 0 10 10" width={9} height={9} stroke="currentColor" strokeWidth={1.4}>
              <path d="M1 1l8 8M9 1l-8 8" />
            </svg>
          </button>
        </span>
      ))}
      <button type="button" onClick={onClearAll}
        className="ml-1 min-h-[28px] cursor-pointer font-sans text-[13px] font-medium text-ink underline underline-offset-2 hover:text-act">
        Clear filters
      </button>
    </div>
  );
}
