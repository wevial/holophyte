import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { createPopup, definePrimitive, popupContent, popupState, popupTrigger } from "@ui-sdk/core";
import { popoverPreset } from "@ui-sdk/core/popover";
import { Checkbox, CheckboxGroup, CheckboxIndicator } from "@ui-sdk/react/checkbox";
import { Listbox, ListboxItem, ListboxItemIndicator } from "@ui-sdk/react/listbox";
import { createParts, usePart, usePrimitive } from "@ui-sdk/react/primitive";
import { Radio, RadioGroup, RadioIndicator } from "@ui-sdk/react/radio";
import { Toggle, ToggleGroup } from "@ui-sdk/react/toggle";
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

function checkedOf(search: PageSearch, param: string): string[] {
  const raw = search[param];
  return typeof raw === "string" ? raw.split(",") : [];
}

function useWrite() {
  const navigate = useNavigate();
  return (change: PageSearch) => void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, page: undefined, ...change }) });
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

function BoxMark({ mixed }: { mixed: boolean }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" className="size-3.5">
      <path d={mixed ? "M4.8 8h6.4" : "M4.6 8.3l2.3 2.3 4.6-4.9"} />
    </svg>
  );
}

/** A label wrapping its control moves focus to it on the click that ends a
 *  press, in the same task as pointerup, so focus returns to `button` in the
 *  task after it. Returns a cancel for a reopen that comes first. */
function refocusAfterPress(button: HTMLElement | undefined) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const stop = () => {
    document.removeEventListener("pointerup", settle, true);
    document.removeEventListener("pointercancel", settle, true);
  };
  const settle = () => {
    stop();
    timer = setTimeout(() => button?.focus({ preventScroll: true }));
  };
  document.addEventListener("pointerup", settle, true);
  document.addEventListener("pointercancel", settle, true);
  return () => (stop(), clearTimeout(timer));
}

const EDGE = 8;
const GAP = 6;

const toolPopover = definePrimitive({
  name: "tool-popover",
  options: { dismissOnFocusLoss: "boolean", onOpenChange: "callback" },
  state: (_options: { dismissOnFocusLoss?: boolean; onOpenChange?: (open: boolean) => void }) => popupState({}),
  api: (context) => {
    let cancelRefocus = () => {};
    context.onDestroy(() => cancelRefocus());
    return createPopup(context, {
      preset: popoverPreset,
      behavior: { dismissOnFocusLoss: !!context.options().dismissOnFocusLoss, blur: context.options().dismissOnFocusLoss ? "always" : "never" },
      rules: (core) => ({
        // Keep the sheet in the page's stacking context so modal overlays cover it.
        bind: () => ({ inline: true }),
        // Controls outside the popover (such as SearchField) preventDefault on Escape,
        // which makes the popup layer skip it; close when Escape did not come from another popup.
        onShow() {
          cancelRefocus();
          const onKey = (event: KeyboardEvent) => {
            if (event.key !== "Escape" || !event.defaultPrevented || event.isComposing) return;
            const dialog = event.target instanceof Element ? event.target.closest('[role="dialog"], [role="alertdialog"], [role="listbox"], [role="menu"]') : null;
            if (dialog && dialog !== core.element("content")) return;
            core.control.hide({ reason: "escape-key", event, focus: true });
          };
          document.addEventListener("keydown", onKey);
          return () => document.removeEventListener("keydown", onKey);
        },
        dismiss(reason, { event, focus }) {
          if (reason !== "outside-press") return void core.control.hide({ reason, event, focus });
          event?.preventDefault();
          core.control.hide({ reason, event, focus: true });
          const label = event?.target instanceof Element ? event.target.closest("label") : null;
          if (label && !label.htmlFor) cancelRefocus = refocusAfterPress(core.triggerElement());
        },
        close(request, close) {
          close();
          if (request.reason !== "escape-key" && request.reason !== "outside-press" && request.reason !== "close-press") return;
          const trigger = core.triggerElement();
          if (trigger && document.activeElement !== trigger) trigger.focus({ preventScroll: true });
        },
      }),
    });
  },
  slots: (slot) => {
    const content = popupContent(slot);
    return {
      root: slot({}),
      trigger: popupTrigger(slot),
      content: slot({ ...content, attrs: (state, part) => ({ ...content.attrs?.(state, part), "aria-labelledby": undefined }) }),
    };
  },
});

const { Trigger: ToolTrigger, Content: ToolContent } = createParts(toolPopover, { trigger: "button", content: "div" });

/** A trigger over a dialog named `label`: Escape and outside presses close it
 *  and return focus to the trigger, focus moving out closes it only with
 *  `dismissOnFocusLoss`, `inPane` keeps it inside its scrolling pane, and
 *  `hidden` closes it. */
export function ToolPopover({
  label, hidden = false, trigger, triggerLabel, triggerTitle, triggerClassName = TOOL, triggerPressed,
  dismissOnFocusLoss, inPane = false, sideOffset = GAP, width, contentRef, className, onOpenChange, children,
}: {
  label: string; hidden?: boolean; trigger: ReactNode; triggerLabel: string; triggerTitle?: string;
  triggerClassName?: string; triggerPressed?: boolean; dismissOnFocusLoss?: boolean; inPane?: boolean; sideOffset?: number;
  width: number; contentRef?: RefObject<HTMLDivElement | null>; className: string;
  onOpenChange?: (open: boolean) => void; children: ReactNode | ((close: () => void) => ReactNode);
}) {
  const handle = usePrimitive(toolPopover, { dismissOnFocusLoss, onOpenChange });
  useEffect(() => {
    if (hidden) handle.api.close();
  }, [hidden, handle]);
  return usePart(handle, "root", {}, {
    className: inPane ? "relative shrink-0" : "shrink-0",
    children: (
      <>
        <ToolTrigger aria-label={triggerLabel} title={triggerTitle} aria-pressed={triggerPressed} className={triggerClassName}>{trigger}</ToolTrigger>
        <ToolContent ref={contentRef} aria-label={label} side="bottom" align="end" sideOffset={sideOffset} collisionPadding={EDGE}
          style={{ zIndex: 20, width: `min(${width}px, var(--available-width, calc(100vw - ${2 * EDGE}px)))`, maxWidth: "var(--available-width)", maxHeight: "var(--available-height)" }}
          className={`${inPane ? "absolute!" : "fixed"} z-20 overflow-y-auto outline-none ${className}`}>
          {typeof children === "function" ? children(() => void handle.api.close({ reason: "close-press" })) : children}
        </ToolContent>
      </>
    ),
  }, "div");
}

export interface FilterSection {
  label: string;
  param: string;
  options: readonly (FilterOption & { count: number; colour?: string })[];
  mono?: boolean;
}

export interface SortSection {
  keys: readonly FilterOption[];
  orders: readonly FilterOption[];
  sort: string;
  order: string;
  onSort: (key: string) => void;
  onOrder: (order: string) => void;
}

const POPOVER_SHEET = "rounded-[9px] border border-hair-2 bg-raised p-2 text-[13px] text-ink-2 shadow-card";
const MENU_HEAD = "flex h-[26px] items-center gap-1.5 px-2 text-[12px] text-ink-3";
const MENU_ROW = "flex h-[26px] cursor-pointer items-center gap-2 rounded-button px-2 text-ink-2 transition-colors hover:bg-hover hover:text-ink has-data-checked:font-medium has-data-checked:text-ink has-focus-visible:outline-2 has-focus-visible:outline-focus";
const MENU_MARK = "inline-flex size-3.5 shrink-0 items-center justify-center outline-none!";

function MenuSection({ section, search, write }: { section: FilterSection; search: PageSearch; write: (change: PageSearch) => void }) {
  const id = useId();
  const raw = checkedOf(search, section.param);
  const allValues = section.options.map((option) => option.value);
  const checked = allValues.filter((value) => raw.includes(value));
  return (
    <CheckboxGroup value={checked} allValues={allValues}
      onValueChange={(values) => write({ [section.param]: values.length > 0 ? values.join(",") : undefined })}
      aria-labelledby={id} render={<fieldset />} className="m-0 min-w-0 border-0 p-0">
      <legend className="float-left w-full px-2 text-[12px]">
        <label className="flex min-h-[26px] cursor-pointer items-center gap-2 font-semibold text-ink">
          <Checkbox parent aria-label={section.label} disabled={section.options.length === 0}
            className="inline-flex size-3.5 shrink-0 cursor-pointer items-center justify-center rounded-[4px] shadow-[inset_0_0_0_1.5px_var(--hair-2)] data-checked:bg-accent data-checked:shadow-none data-indeterminate:bg-accent data-indeterminate:shadow-none disabled:cursor-default disabled:opacity-50 forced-colors:border forced-colors:border-solid">
            <CheckboxIndicator className="inline-flex items-center justify-center text-raised">
              {(state) => <BoxMark mixed={state.indeterminate} />}
            </CheckboxIndicator>
          </Checkbox>
          <span id={id}>{section.label}</span>{" "}
          {checked.length > 0 && <span className="ml-auto font-mono text-[12px] font-normal text-ink-3">{checked.length} of {section.options.length}</span>}
        </label>
      </legend>
      {section.options.map((option) => (
        <label key={option.value} className={`${MENU_ROW} ${option.count === 0 ? "text-ink-3" : ""}`}>
          <Checkbox value={option.value} aria-label={option.label} className={MENU_MARK}>
            <CheckboxIndicator className="inline-flex items-center justify-center"><CheckMark /></CheckboxIndicator>
          </Checkbox>
          {option.colour && <i aria-hidden="true" className="size-[7px] shrink-0 rounded-full" style={{ background: option.colour }} />}
          <span data-option-label className={`min-w-0 truncate ${section.mono ? "font-mono text-[12px]" : ""}`}>{option.label}</span>{" "}
          <span className="ml-auto font-mono text-[12px] font-normal tabular-nums text-ink-3">{option.count}</span>
        </label>
      ))}
    </CheckboxGroup>
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
        <ToggleGroup value={[sorting.order]} deselectable={false} onValueChange={([next]) => { if (next) sorting.onOrder(next); }}
          render={({ role: _role, ...props }) => <span {...props} />} className="ml-auto inline-flex gap-0.5">
          {sorting.orders.map((order) => (
            <Toggle key={order.value} value={order.value} aria-label={order.label} title={order.label}
              className="inline-grid size-[22px] cursor-pointer place-items-center rounded-[5px] text-ink-3 hover:bg-hover hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink">
              <OrderArrow down={order.value === "desc"} />
            </Toggle>
          ))}
        </ToggleGroup>
      </legend>
      <RadioGroup value={sorting.sort} onValueChange={(next) => { if (next) sorting.onSort(next); }} aria-labelledby={id}>
        {sorting.keys.map((key) => (
          <label key={key.value} className={MENU_ROW}>
            <Radio value={key.value} aria-label={key.label} className={MENU_MARK}>
              <RadioIndicator className="inline-flex items-center justify-center"><CheckMark /></RadioIndicator>
            </Radio>
            {key.label}
          </label>
        ))}
      </RadioGroup>
    </fieldset>
  );
}

function stackSheet(node: HTMLElement) {
  const scrolled = node.scrollTop;
  node.removeAttribute("data-stacked");
  if ([...node.querySelectorAll("[data-option-label]")].some((option) => option.scrollWidth > option.clientWidth)) node.setAttribute("data-stacked", "");
  node.scrollTop = scrolled;
}

/** A Filter button over a dialog of checkbox `sections`, each section's
 *  checked values written to its own search param as a comma list in the
 *  section's order, after an optional `sorting` and `extra`; `hidden`
 *  closes it. The trigger is named `label` and the count of filters on,
 *  with a dot while any is. */
export function FilterPopover({ label, sections, hidden, sorting, extra }: {
  label: string; sections: readonly FilterSection[]; hidden: boolean; sorting?: SortSection; extra?: ReactNode;
}) {
  const search = useSearch({ strict: false }) as PageSearch;
  const write = useWrite();
  const [open, setOpen] = useState(false);
  const count = sections.reduce((sum, section) => sum + section.options.filter((option) => checkedOf(search, section.param).includes(option.value)).length, 0);
  const on = count === 1 ? "1 filter on" : `${count} filters on`;
  const sheet = useRef<HTMLDivElement>(null);
  const width = useRef<number | null>(null);
  // Checked on open, after picks and when the sheet's width changes; rAF keeps height changes out of ResizeObserver's delivery pass.
  useLayoutEffect(() => {
    const node = sheet.current;
    if (!open || !node) return;
    stackSheet(node);
    width.current = node.clientWidth;
  });
  useLayoutEffect(() => {
    const node = sheet.current;
    if (!open || !node || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      if (node.clientWidth === width.current) return;
      frame ||= requestAnimationFrame(() => {
        frame = 0;
        if (node.clientWidth === width.current) return;
        stackSheet(node);
        width.current = node.clientWidth;
      });
    });
    observer.observe(node);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [open]);
  return (
    <ToolPopover
      label={label}
      hidden={hidden}
      onOpenChange={setOpen}
      triggerLabel={count > 0 ? `${label}, ${on}` : label}
      triggerTitle={label}
      trigger={<><ToolFilterIcon />{count > 0 && <i data-filter-dot aria-hidden="true" className="absolute top-1 right-1 size-1.5 rounded-full bg-accent shadow-[0_0_0_2px_var(--canvas)]" />}</>}
      contentRef={sheet}
      width={600}
      className={`group/sheet grid ${sorting || extra ? "grid-cols-3" : "grid-cols-2"} gap-x-2.5 ${POPOVER_SHEET} data-stacked:grid-cols-1`}
    >
      {(sorting || extra) && (
        <div>
          {sorting && <MenuSorting sorting={sorting} />}
          {extra}
        </div>
      )}
      <div className="col-span-2 grid grid-cols-2 grid-rows-[auto_1fr] items-start gap-x-2.5 gap-y-2 group-data-stacked/sheet:col-span-1 group-data-stacked/sheet:grid-cols-1 group-data-stacked/sheet:grid-rows-none [&>fieldset:nth-of-type(2)]:col-start-2 [&>fieldset:nth-of-type(2)]:row-span-2 group-data-stacked/sheet:[&>fieldset:nth-of-type(2)]:col-start-1 group-data-stacked/sheet:[&>fieldset:nth-of-type(2)]:row-span-1">
        {sections.map((section) => <MenuSection key={section.param} section={section} search={search} write={write} />)}
      </div>
      <div className="col-span-full mt-1.5 flex justify-end border-t border-hair pt-1.5">
        <button type="button" disabled={count === 0} onClick={() => write(Object.fromEntries(sections.map((section) => [section.param, undefined])))}
          className="h-[26px] cursor-pointer rounded-button px-2 text-[12px] font-medium text-ink-2 hover:bg-hover hover:text-ink disabled:cursor-default disabled:text-ink-3 disabled:hover:bg-transparent">
          Clear
        </button>
      </div>
    </ToolPopover>
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
      <ToggleGroup aria-labelledby={id} value={[String(value)]} deselectable={false}
        onValueChange={([next]) => { const picked = options.find((option) => String(option) === next); if (picked !== undefined) onPick(picked); }}
        className="mx-2 mt-0.5 inline-flex rounded-[7px] bg-canvas p-0.5">
        {options.map((option) => (
          <Toggle key={String(option)} value={String(option)}
            className="h-6 cursor-pointer rounded-[5px] px-2.5 font-mono text-[12px] font-medium text-ink-3 hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink">
            {option}
          </Toggle>
        ))}
      </ToggleGroup>
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

const RANGE_WIDTH = 248;

/** A group of `presets` written as the search's `range`, `fallback`
 *  dropping it, and a Custom range popover writing `from` and `to`, which
 *  win over `range` while set; `hidden` closes it. */
export function RangeButtons({ label, presets, fallback, hidden }: {
  label: string; presets: readonly FilterOption[]; fallback: string; hidden: boolean;
}) {
  const { range, from, to } = useSearch({ strict: false }) as { range?: string; from?: string; to?: string };
  const write = useWrite();
  const custom = from != null || to != null;
  const fromId = useId();
  const toId = useId();
  return (
    <div className="flex max-w-full flex-wrap items-center gap-1.5">
      <Segmented label={label} options={presets} value={custom ? undefined : (range ?? fallback)}
        onPick={(picked) => write({ range: picked === fallback ? undefined : picked, from: undefined, to: undefined })} />
      <ToolPopover
        label="Custom range"
        hidden={hidden}
        inPane
        triggerLabel="Custom range"
        triggerTitle="Custom range"
        triggerPressed={custom}
        triggerClassName={`${TOOL} aria-pressed:bg-selected aria-pressed:text-ink`}
        trigger={
          <>
            <ToolCalendarIcon />
            {custom && <span className="font-mono text-[12px] tabular-nums whitespace-nowrap text-accent">{dayRangeLabel(from, to)}</span>}
          </>
        }
        width={RANGE_WIDTH}
        className={`flex flex-col gap-2 ${POPOVER_SHEET}`}
      >
        {(close) => (
          <>
            <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1.5 px-2 pt-0.5">
              <label htmlFor={fromId} className="text-[12px] text-ink-3">From</label>
              <input id={fromId} type="date" value={from ?? ""} max={to} className={DATE_FIELD}
                onChange={(event) => write({ range: undefined, from: event.target.value || undefined })} />
              <label htmlFor={toId} className="text-[12px] text-ink-3">To</label>
              <input id={toId} type="date" value={to ?? ""} min={from} className={DATE_FIELD}
                onChange={(event) => write({ range: undefined, to: event.target.value || undefined })} />
            </div>
            <Btn onClick={close} className="self-end">Done</Btn>
          </>
        )}
      </ToolPopover>
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
  const custom = from != null || to != null;
  const picked = custom ? undefined : (presets.find((preset) => preset.value === (range ?? fallback)) ?? presets.find((preset) => preset.value === fallback));
  const shownFrom = custom ? from : picked?.back != null ? dayBefore(now, picked.back) : undefined;
  const shownTo = custom ? to : dayBefore(now, 0);
  const fromId = useId();
  const toId = useId();
  return (
    <ToolPopover
      label="Range"
      hidden={hidden}
      inPane
      triggerLabel={`Range: ${picked?.name ?? dayRangeLabel(from, to)}`}
      triggerTitle={picked?.name ?? "Custom range"}
      trigger={
        <>
          <ToolCalendarIcon />
          <span className="font-mono text-[12px] tabular-nums whitespace-nowrap text-accent">{picked?.label ?? dayRangeLabel(from, to)}</span>
        </>
      }
      width={RANGE_WIDTH}
      className={POPOVER_SHEET}
    >
      {(close) => (
        <>
          <Listbox
            aria-label="Started"
            activation="manual"
            deselectable
            value={picked?.value ?? null}
            onValueChange={(next, details) => {
              const value = next ?? picked?.value;
              if (next == null) details.cancel();
              if (value != null) write({ range: value === fallback ? undefined : value, from: undefined, to: undefined });
              close();
            }}
            className="flex flex-col outline-none"
          >
            {presets.map((preset) => (
              <ListboxItem
                key={preset.value}
                value={preset.value}
                className="flex h-[26px] w-full cursor-pointer items-center gap-2 rounded-button px-2 text-left text-ink-2 outline-none hover:bg-hover hover:text-ink data-selected:font-medium data-selected:text-ink focus-visible:bg-hover focus-visible:text-ink focus-visible:outline-2 focus-visible:outline-focus"
              >
                <span aria-hidden="true" className={MENU_MARK}>
                  <ListboxItemIndicator className="inline-flex items-center justify-center"><CheckMark /></ListboxItemIndicator>
                </span>
                {preset.name}
              </ListboxItem>
            ))}
          </Listbox>
          <div className="my-1.5 h-px bg-hair" />
          <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1.5 px-2 pt-0.5 pb-1">
            <label htmlFor={fromId} className="text-[12px] text-ink-3">From</label>
            <input id={fromId} type="date" value={shownFrom ?? ""} max={shownTo} className={DATE_FIELD}
              onChange={(event) => write({ range: undefined, from: event.target.value || undefined, to: shownTo })} />
            <label htmlFor={toId} className="text-[12px] text-ink-3">To</label>
            <input id={toId} type="date" value={shownTo ?? ""} min={shownFrom} className={DATE_FIELD}
              onChange={(event) => write({ range: undefined, from: shownFrom, to: event.target.value || undefined })} />
          </div>
        </>
      )}
    </ToolPopover>
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
