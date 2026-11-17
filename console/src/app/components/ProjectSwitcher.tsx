import {
  createListbox,
  createPopup,
  definePrimitive,
  popupContent,
  popupState,
  popupTitle,
  popupTrigger,
  selects,
  type SlotFactory,
} from "@ui-sdk/core";
import { popoverPreset } from "@ui-sdk/core/popover";
import type { BaseSlotProps } from "@ui-sdk/core/renderer";
import { createParts, usePart, usePrimitive } from "@ui-sdk/react/primitive";
import { useId, useMemo, useRef } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { storeScope, type SwitcherRow } from "../projects";
import type { ScopeSearch } from "../scope";
import { DropIcon } from "./icons";

const DIALOG_WIDTH = 264;
const EDGE = 8;
const GAP = 4;

type SwitcherOptions = {
  defaultOpen?: boolean;
  items?: { value: string; label: string }[];
  current?: string;
  onChoose?: (key: string) => void;
};

/** What a filtering combobox's primitive offers its input and options. */
export interface ComboApi<State extends object> {
  listbox: ReturnType<typeof createListbox<State>>;
  search(query: string): void;
  keydown(event: KeyboardEvent): void;
  choose(key: string | null, event?: Event): void;
}

/** The combobox input of a list whose highlight is virtual: it holds the
 *  query, points at the highlighted option and keys through `api.keydown`. */
export function comboInput<State extends { query: string }, Api extends ComboApi<State>>(slot: SlotFactory<State, Api>) {
  return slot({
    role: "combobox",
    focus: "native",
    keys: ["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Enter"],
    attrs: (state, { id, api }) => ({
      value: state.query,
      "aria-expanded": "true",
      "aria-controls": `${id}-list`,
      "aria-autocomplete": "list",
      "aria-activedescendant": api.listbox.activeId(state),
    }),
    on: {
      input: (event, { api }) => api.search((event.target as HTMLInputElement).value),
      keydown: (event, { api }) => api.keydown(event as KeyboardEvent),
    },
  });
}

type OptionProps = { value: string; disabled?: boolean } & BaseSlotProps;

/** An option of that list: highlighted as the pointer moves, chosen on a
 *  press, `aria-selected` where `selected` says, re-rendered as `reads` selects. */
export function virtualOption<State extends { highlighted: string | null }, Api extends ComboApi<State>>(
  slot: SlotFactory<State, Api>,
  selected: (state: State, key: string) => boolean,
  reads: ReturnType<typeof selects<State, Api, OptionProps>>,
) {
  return slot<{ value: string; disabled?: boolean }>({
    role: "option",
    focus: "active-descendant",
    props: { value: "string", disabled: "boolean" },
    selects: reads,
    attrs: (state, part) => ({
      ...part.api.listbox.attrs(state, part),
      "aria-selected": String(selected(state, part.key)),
      "data-active": String(part.key === state.highlighted),
    }),
    press: ({ key, api }, event) => api.choose(key, event),
    on: { pointermove: (event, { key, api }) => api.listbox.highlight(key, { event }) },
    bind: (el, part) => {
      if (part.key === part.store.get().highlighted) el.scrollIntoView?.({ block: "nearest" });
      return part.api.listbox.bind(el, part);
    },
  });
}

const switcher = definePrimitive({
  name: "project-switcher",
  options: { defaultOpen: "boolean", items: "value", current: "string", onChoose: "callback" },
  state: (options: SwitcherOptions) => ({
    ...popupState(options),
    query: "",
    highlighted: null as string | null,
    current: options.current ?? null,
  }),
  sync: (options) => ({ current: options.current ?? null }),
  api: (context) => {
    const popup = createPopup(context, { preset: popoverPreset });
    const listbox = createListbox(context, {
      key: "highlighted",
      focus: "virtual",
      items: () => context.options().items ?? [],
      query: () => context.store.get().query.trim(),
      filter: () => (item, query) => item.label.toLowerCase().includes(query.toLowerCase()),
    });
    let wasOpen = context.store.get().open;
    context.onDestroy(
      context.store.subscribe(() => {
        const { open, current } = context.store.get();
        if (open === wasOpen) return;
        wasOpen = open;
        if (!open) return;
        context.store.set({ query: "" });
        if (context.options().items?.some((item) => item.value === current)) listbox.highlight(current);
        else listbox.move("first");
      }),
    );
    const choose = (key: string | null, event?: Event) => {
      if (key == null) return;
      popup.close({ reason: "item-press", event });
      context.options().onChoose?.(key);
    };
    return {
      ...popup,
      listbox,
      choose,
      search(query: string) {
        context.store.set({ query });
        listbox.move("first");
      },
      keydown(event: KeyboardEvent) {
        if (event.key === "Tab" && event.shiftKey) setTimeout(() => popup.close({ reason: "focus-out", event }));
        if (event.key !== "Enter") return listbox.keydown(event);
        event.preventDefault();
        choose(context.store.get().highlighted, event);
      },
    };
  },
  slots: (slot) => ({
    root: slot({}),
    trigger: popupTrigger(slot),
    content: popupContent(slot),
    title: popupTitle(slot),
    input: comboInput(slot),
    list: slot<{}, { shown: string[] }>({
      role: "listbox",
      state: (_state, { api }) => ({ shown: api.listbox.items().map((item) => (item as { value: string }).value) }),
      keys: { none: "The combobox's input handles them." },
      present: (_state, { api }) => api.listbox.count() > 0,
      attrs: (_state, { id }) => ({ id: `${id}-list` }),
    }),
    empty: slot({ present: (_state, { api }) => api.listbox.count() === 0 }),
    option: virtualOption(slot, (state, key) => key === state.current, selects({ highlighted: ({ key }) => [key, null] })),
  }),
});

const { Trigger, Content, Title, Input, List, Empty, Option } = createParts(switcher, {
  trigger: "button",
  content: "div",
  title: "h2",
  input: "input",
  list: "ul",
  empty: "p",
  option: "li",
});

type Dot = { colour: string; down: boolean };

function ScopeDots({ dots }: { dots: Dot[] }) {
  return (
    <span data-dots aria-hidden="true" className="flex shrink-0">
      {dots.map(({ colour, down }, at) => (
        <i
          key={at}
          data-dot
          data-down={down || undefined}
          style={{
            background: down ? "transparent" : colour,
            boxShadow: down ? "inset 0 0 0 1.5px var(--bad), 0 0 0 2px var(--dot-ring)" : "0 0 0 2px var(--dot-ring)",
          }}
          className="-mr-0.5 size-2 rounded-full last:mr-0"
        />
      ))}
    </span>
  );
}

function CheckIcon() {
  return (
    <svg data-check viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-3.5 shrink-0 text-accent">
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" aria-hidden="true" className="size-4 shrink-0 text-ink-3">
      <circle cx="7" cy="7" r="4.5" />
      <path d="m10.5 10.5 3 3" />
    </svg>
  );
}

function HeldTag() {
  return <span className="font-mono text-[11px] font-normal text-wait">held</span>;
}

function buttonName(row: SwitcherRow): string {
  const facts = [row.label];
  if (row.held) facts.push("held");
  if (row.down) facts.push("not answering");
  if (row.need > 0) facts.push(`${row.need} waiting on you`);
  return `Project: ${facts.join(", ")}. Switch project`;
}

function unknownRow(name: string): SwitcherRow {
  return { name, label: name, held: false, need: 0, open: 0, down: false };
}

const rowKey = (row: SwitcherRow) => (row.name == null ? "all-projects-scope" : `project:${row.name}`);

export function ProjectSwitcher({ rows, colours, folded }: { rows: SwitcherRow[]; colours: Map<string, string>; folded: boolean }) {
  const { project } = useSearch({ from: "__root__" });
  const navigate = useNavigate();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const findId = `${useId()}-find`;

  const current = rows.find((row) => row.name === (project ?? null)) ?? unknownRow(project!);
  const byKey = useMemo(() => new Map(rows.map((row) => [rowKey(row), row])), [rows]);
  const dotsOf = (row: SwitcherRow): Dot[] =>
    (row.name == null ? rows : [row]).flatMap((each) => {
      const colour = each.name == null ? undefined : colours.get(each.name);
      return colour == null ? [] : [{ colour, down: row.name != null && each.down }];
    });
  const items = useMemo(() => rows.map((row) => ({ value: rowKey(row), label: row.label })), [rows]);

  const handle = usePrimitive(switcher, {
    items,
    current: rowKey(current),
    onChoose: (key) => {
      const row = byKey.get(key)!;
      storeScope(row.name);
      void navigate({ to: ".", search: (previous: ScopeSearch) => ({ ...previous, project: row.name ?? undefined }) });
    },
  });

  const besideRailWhenRoom = {
    getBoundingClientRect: () => {
      const rect = buttonRef.current!.getBoundingClientRect();
      const rail = buttonRef.current!.closest("aside")?.getBoundingClientRect() ?? rect;
      const beside = rail.right + GAP <= window.innerWidth - DIALOG_WIDTH - EDGE;
      return beside ? new DOMRect(rail.right + GAP, rect.top - GAP, 0, 0) : rect;
    },
  };

  return usePart(
    handle,
    "root",
    {},
    {
      className: `pb-1 ${folded ? "flex justify-center pt-3" : "px-2"}`,
      children: (
        <>
          <Trigger
            ref={buttonRef}
            aria-label={buttonName(current)}
            title={folded ? current.label : undefined}
            onKeyDown={(event) => {
              if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
              event.preventDefault();
              handle.api.setOpen(true, { event: event.nativeEvent });
            }}
            className={`relative flex items-center gap-2 rounded-[6px] text-[13px] text-ink [--dot-ring:var(--side)] hover:bg-hover hover:[--dot-ring:var(--hover)] aria-expanded:bg-hover aria-expanded:[--dot-ring:var(--hover)] ${
              folded ? "size-11 justify-center" : "h-row w-full px-2.5"
            }`}
          >
            <ScopeDots dots={dotsOf(current)} />
            {!folded && (
              <>
                <span className="min-w-0 flex-1 truncate text-left font-medium">{current.label}</span>
                {current.held && <HeldTag />}
                <span className="text-ink-3">
                  <DropIcon />
                </span>
              </>
            )}
          </Trigger>

          <Content
            side="bottom"
            align="start"
            sideOffset={GAP}
            collisionPadding={EDGE}
            anchor={folded ? besideRailWhenRoom : undefined}
            onFocus={(event) => {
              if (event.target === event.currentTarget) document.getElementById(findId)?.focus();
            }}
            style={{ maxHeight: "var(--available-height)", width: `min(${DIALOG_WIDTH}px, calc(100vw - ${2 * EDGE}px))` }}
            className="fixed z-50 flex flex-col rounded-[10px] bg-raised p-1 shadow-[0_0_0_1px_var(--hair-2),var(--card-shadow)]"
            onMouseDown={(event) => {
              if (event.target !== event.currentTarget.querySelector("input")) event.preventDefault();
            }}
          >
            <Title className="sr-only">Switch project</Title>
            <label htmlFor={findId} className="sr-only">
              Find a project
            </label>
            <div className="mb-1 flex h-[34px] shrink-0 items-center gap-2 border-b border-hair px-2">
              <SearchIcon />
              <Input
                id={findId}
                placeholder="Find a project"
                autoComplete="off"
                className="h-full min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none! placeholder:text-ink-3"
              />
            </div>
            <Empty className="px-2 py-2.5 text-[13px] text-ink-3">No project matches.</Empty>
            <List aria-label="Projects" className="flex max-h-[320px] min-h-0 flex-col gap-px overflow-y-auto">
              {({ shown }) =>
                shown.map((key) => {
                  const row = byKey.get(key)!;
                  return (
                    <Option
                      key={key}
                      value={key}
                      className="flex h-8 shrink-0 cursor-pointer items-center gap-[9px] rounded-[6px] px-2 text-[13px] font-medium text-ink-2 [--dot-ring:var(--raised)] aria-selected:text-ink data-[active=true]:bg-hover data-[active=true]:text-ink data-[active=true]:[--dot-ring:var(--hover)]"
                    >
                      <ScopeDots dots={dotsOf(row)} />
                      <span className="min-w-0 flex-1 truncate">
                        {row.label}
                        {row.down && <span className="sr-only"> not answering</span>}
                      </span>
                      {row.held && <span className="text-[11.5px] text-ask-ink">held</span>}
                      {key === rowKey(current) ? <CheckIcon /> : <span className="size-3.5 shrink-0" />}
                    </Option>
                  );
                })
              }
            </List>
          </Content>
        </>
      ),
    },
    "div",
  );
}
