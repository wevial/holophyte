import { createListbox, createPopup, definePrimitive, popupContent, popupState, popupTrigger, selects } from "@ui-sdk/core";
import { popoverPreset } from "@ui-sdk/core/popover";
import { createParts, usePart, usePrimitive } from "@ui-sdk/react/primitive";
import { useId } from "react";
import { DropIcon } from "./icons";

const EDGE = 8;
const GAP = 4;
const MENU_WIDTH = 220;

export interface PickerOption {
  value: string;
  label: string;
  disabled?: boolean;
  note?: string;
}

type PickerOptions = {
  defaultOpen?: boolean;
  items?: PickerOption[];
  current?: string;
  onChoose?: (key: string) => void;
  passPress?: boolean;
};

const LISTBOX_POPOVER = { ...popoverPreset, role: undefined };

/** A label wrapping its control moves focus to it on the click that ends a
 *  press, in the same task as the press's pointerup, so focus goes back to
 *  `button` in the task after it. Returns a cancel for a reopen that comes
 *  first. A label naming another control by `for` keeps that control's focus. */
function refocusAfterPress(button: HTMLElement | undefined) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const settle = () => {
    stop();
    timer = setTimeout(() => button?.focus({ preventScroll: true }));
  };
  const stop = () => {
    document.removeEventListener("pointerup", settle, true);
    document.removeEventListener("pointercancel", settle, true);
  };
  document.addEventListener("pointerup", settle, true);
  document.addEventListener("pointercancel", settle, true);
  return () => {
    stop();
    clearTimeout(timer);
  };
}

const picker = definePrimitive({
  name: "picker",
  options: {
    defaultOpen: "boolean",
    items: "value",
    current: "string",
    onChoose: "callback",
    passPress: "boolean",
  },
  state: (options: PickerOptions) => ({
    ...popupState(options),
    highlighted: null as string | null,
    current: options.current ?? null,
  }),
  sync: (options) => ({ current: options.current ?? null }),
  api: (context) => {
    let cancelRefocus = () => {};
    const popup = createPopup(context, {
      preset: LISTBOX_POPOVER,
      rules: (core) => ({
        dismiss(reason, { event, focus }) {
          if (reason !== "outside-press") return void core.control.hide({ reason, event, focus });
          if (context.options().passPress) return void core.control.hide({ reason, event, focus: false });
          event?.preventDefault();
          core.control.hide({ reason, event, focus: true });
          const label = event?.target instanceof Element ? event.target.closest("label") : null;
          if (label && !label.htmlFor) cancelRefocus = refocusAfterPress(core.triggerElement());
        },
      }),
    });
    const listbox = createListbox(context, {
      key: "highlighted",
      items: () => context.options().items ?? [],
    });
    let wasOpen = context.store.get().open;
    context.onDestroy(() => cancelRefocus());
    context.onDestroy(
      context.store.subscribe(() => {
        const { open, current } = context.store.get();
        if (open === wasOpen) return;
        wasOpen = open;
        if (!open) return;
        cancelRefocus();
        if (context.options().items?.some((item) => item.value === current)) listbox.highlight(current);
        else listbox.move("first");
      }),
    );
    const choose = (key: string | null, event?: Event) => {
      if (key == null || context.options().items?.find((item) => item.value === key)?.disabled) return;
      popup.close({ reason: "item-press", event });
      context.options().onChoose?.(key);
    };
    return {
      ...popup,
      listbox,
      choose,
      keydown(event: KeyboardEvent) {
        if (event.key !== "Enter" && event.key !== " ") return listbox.keydown(event);
        event.preventDefault();
        choose(context.store.get().highlighted, event);
      },
    };
  },
  slots: (slot) => ({
    root: slot({}),
    trigger: popupTrigger(slot),
    content: popupContent(slot),
    list: slot({
      role: "listbox",
      keys: { none: "The content's keydown moves the highlight." },
    }),
    option: slot<{ value: string }>({
      role: "option",
      focus: "roving",
      props: { value: "string" },
      selects: selects({ highlighted: ({ key }) => [key, null] }),
      attrs: (state, part) => ({
        ...part.api.listbox.attrs(state, part),
        "aria-selected": String(part.key === state.current),
        "data-active": String(part.key === state.highlighted),
      }),
      press: ({ key, api }, event) => api.choose(key, event),
      on: {
        pointermove: (event, { key, api }) => api.listbox.highlight(key, { event }),
      },
      bind: (el, part) => part.api.listbox.bind(el, part),
    }),
  }),
});

const { Trigger, Content, List, Option } = createParts(picker, {
  trigger: "button",
  content: "div",
  list: "div",
  option: "div",
});

function CheckIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      width={14}
      height={14}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="size-3.5 shrink-0"
    >
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
  );
}

/** A button reading the current option that opens a listbox of `options`;
 *  picking one calls `onPick` with its value. `passPress` lets a press
 *  outside the open list close it and keep its own effect, so a press in
 *  another field focuses that field. */
export function Picker({ id, label, options, value, onPick, passPress, "aria-invalid": invalid, "aria-describedby": describedBy }: {
  id?: string; label: string; options: readonly PickerOption[]; value: string; onPick: (value: string) => void; passPress?: boolean; "aria-invalid"?: boolean; "aria-describedby"?: string;
}) {
  const current = options.find((option) => option.value === value) ?? options[0];
  const labelId = useId();
  const handle = usePrimitive(picker, {
    items: options as PickerOption[],
    current: current?.value,
    onChoose: onPick,
    passPress,
  });
  return usePart(
    handle,
    "root",
    {},
    {
      className: "inline-flex max-w-full min-w-0",
      children: (
        <>
          <span id={labelId} hidden>
            {label}
          </span>
          <Trigger
            id={id}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            aria-haspopup="listbox"
            aria-label={`${label}: ${current?.label ?? ""}${current?.note ? ` ${current.note}` : ""}`}
            onKeyDown={(event) => {
              if (event.key !== "ArrowDown") return;
              event.preventDefault();
              handle.api.setOpen(true, { event: event.nativeEvent });
            }}
            className="inline-flex min-h-[30px] max-w-full min-w-0 shrink-0 cursor-pointer items-center gap-2 rounded-button border-0 bg-selected py-1.5 pr-2 pl-2.5 text-left text-[13px] leading-[1.25] font-medium text-ink hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus aria-[invalid=true]:shadow-[inset_0_0_0_1px_var(--bad)]"
          >
            <span className="min-w-0 [overflow-wrap:anywhere]">{current?.label}</span>
            {current?.note && <span className="shrink-0 whitespace-nowrap text-ink-3">{" "}{current.note}</span>}
            <span className="flex shrink-0 text-ink-3">
              <DropIcon />
            </span>
          </Trigger>
          <Content
            side="bottom"
            align="start"
            sideOffset={GAP}
            collisionPadding={EDGE}
            onFocus={(event) => {
              if (event.target === event.currentTarget) event.currentTarget.querySelector<HTMLElement>("[data-highlighted]")?.focus();
            }}
            onKeyDown={(event) => handle.api.keydown(event.nativeEvent)}
            style={{
              maxHeight: "var(--available-height)",
              width: `min(${MENU_WIDTH}px, calc(100vw - ${2 * EDGE}px))`,
            }}
            className="fixed z-50 flex flex-col overflow-y-auto rounded-[9px] bg-raised p-1 shadow-[0_0_0_1px_var(--hair-2),var(--card-shadow)] outline-none"
          >
            <List aria-labelledby={labelId} className="flex flex-col">
              {options.map((option) => (
                <Option
                  key={option.value}
                  value={option.value}
                  aria-disabled={option.disabled || undefined}
                  className="flex min-h-[30px] shrink-0 cursor-pointer items-center gap-2 rounded-button px-2 py-1.5 text-[13px] leading-[1.25] text-ink-2 data-[active=true]:bg-hover data-[active=true]:text-ink aria-disabled:cursor-default aria-disabled:text-ink-3"
                >
                  <span className="min-w-0 [overflow-wrap:anywhere]">{option.label}</span>
                  {option.note && <span className="shrink-0 whitespace-nowrap text-ink-3">{" "}{option.note}</span>}
                  <span className="flex-1" />
                  <span className={`flex text-accent ${option.value === current?.value ? "" : "invisible"}`}>
                    <CheckIcon />
                  </span>
                </Option>
              ))}
            </List>
          </Content>
        </>
      ),
    },
    "div",
  );
}
