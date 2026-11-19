import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

/** One entry of a ⋯ menu; `note` reads under it, and a `divided` entry
 *  sits after a rule. */
export interface MenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  note?: string;
  divided?: boolean;
}

/** A row's ⋯ menu shows while the row (a `group`) is hovered or holds
 *  focus, while the menu is open, and always where nothing can hover. */
export const ROW_MENU = "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 has-[[aria-expanded=true]]:z-50 has-[[aria-expanded=true]]:opacity-100 [@media(hover:none)]:opacity-100";

const TRIGGER = "inline-flex items-center justify-center rounded-button text-ink-2 hover:bg-hover hover:text-ink aria-expanded:bg-selected aria-expanded:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

function DotsIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={16} height={16} fill="currentColor" className="size-4 shrink-0">
      <circle cx="3.5" cy="8" r="1.25" />
      <circle cx="8" cy="8" r="1.25" />
      <circle cx="12.5" cy="8" r="1.25" />
    </svg>
  );
}

/** A ⋯ button named `label` opening a menu of `items`, under the button,
 *  above it where the viewport has no room below, else held inside the
 *  viewport and scrolling where it is taller; kept inside it sideways and
 *  placed again when its items change. `onOpenChange` hears it open and
 *  close. Arrow keys, Home and End move between items; Esc or a choice
 *  closes it and returns focus to the button, and focus lost with an item
 *  that left goes to the menu. A pointer opens it with no item focused,
 *  keys with the first or last. Its clicks never reach the row it sits
 *  in; `compact` is a row's smaller unedged button. */
export function MoreMenu({ label, items, className = "", compact = false, onOpenChange }: {
  label: string; items: MenuItem[]; className?: string; compact?: boolean; onOpenChange?: (open: boolean) => void;
}) {
  // Positions are kept in zoomed viewport pixels, as rectangles report them; `scale` turns them into the menu's own CSS pixels.
  const [at, setAt] = useState<{ top: number; right: number; above: number; below: number; side: number; scale: number; placed?: boolean; maxHeight?: number } | null>(null);
  const [focusOn, setFocusOn] = useState<"menu" | "first" | "last">("menu");
  const id = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const open = at != null;
  const entries = () => [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];

  useLayoutEffect(() => {
    const box = menu.current?.getBoundingClientRect();
    if (at == null || at.placed || box == null) return;
    const right = box.left < 8 ? Math.max(8, window.innerWidth - 8 - box.width) : at.right;
    if (box.bottom <= window.innerHeight - 8) {
      if (right !== at.right) setAt({ ...at, placed: true, right });
      return;
    }
    const room = window.innerHeight - 16;
    if (at.above - box.height >= 8) setAt({ ...at, placed: true, right, top: at.above - box.height });
    else setAt({ ...at, placed: true, right, top: Math.max(8, room + 8 - box.height), maxHeight: box.height > room ? room : undefined });
  }, [at]);

  const shape = items.map((item) => `${item.label}|${item.note ?? ""}|${item.disabled === true}`).join("\n");
  useLayoutEffect(() => {
    setAt((now) => (now == null ? now : { ...now, top: now.below, right: now.side, maxHeight: undefined, placed: false }));
  }, [shape]);

  useEffect(() => {
    onOpenChange?.(open);
  }, [open]);

  useEffect(() => {
    if (open && (document.activeElement == null || document.activeElement === document.body)) menu.current?.focus();
  });

  useEffect(() => {
    if (!open) return;
    const all = entries();
    if (focusOn === "menu") menu.current?.focus();
    else (focusOn === "first" ? all[0] : all.at(-1))?.focus();
    const outside = (event: Event) => {
      if (!wrap.current?.contains(event.target as Node)) setAt(null);
    };
    const away = () => setAt(null);
    const scrolled = (event: Event) => {
      if (!menu.current?.contains(event.target as Node)) setAt(null);
    };
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("resize", away);
    window.addEventListener("scroll", scrolled, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("resize", away);
      window.removeEventListener("scroll", scrolled, true);
    };
  }, [open, focusOn]);

  const show = (focus: "menu" | "first" | "last") => {
    const box = button.current!.getBoundingClientRect();
    const width = button.current!.offsetWidth;
    const gap = compact ? 3 : 6;
    setFocusOn(focus);
    const scale = width > 0 && box.width > 0 ? box.width / width : 1;
    const side = Math.max(8, window.innerWidth - box.right - (compact ? 4 : 0) * scale);
    setAt({ top: box.bottom + gap * scale, right: side, above: box.top - gap * scale, below: box.bottom + gap * scale, side, scale });
  };
  const close = () => {
    setAt(null);
    button.current?.focus();
  };
  const choose = (item: MenuItem) => {
    if (item.disabled) return;
    close();
    item.onSelect();
  };
  const onButtonKey = (event: KeyboardEvent) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    show(event.key === "ArrowDown" ? "first" : "last");
  };
  const onMenuKey = (event: KeyboardEvent) => {
    const all = entries();
    const now = all.indexOf(document.activeElement as HTMLElement);
    const step: Record<string, number> = { ArrowDown: now + 1, ArrowUp: now < 0 ? all.length - 1 : now - 1, Home: 0, End: all.length - 1 };
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === "Tab") {
      setAt(null);
    } else if (event.key in step) {
      event.preventDefault();
      all[(step[event.key]! + all.length) % all.length]?.focus();
    }
  };

  return (
    <div ref={wrap} className={className} onClick={(event) => event.stopPropagation()}>
      <button
        ref={button}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => (open ? setAt(null) : show("menu"))}
        onKeyDown={onButtonKey}
        className={`${TRIGGER} ${compact ? "size-[26px] aria-expanded:bg-[color-mix(in_oklab,var(--accent)_32%,var(--selected))]" : "size-8 shadow-[inset_0_0_0_1px_var(--hair-2)]"}`}
      >
        <DotsIcon />
      </button>
      {at != null && (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label={label}
          tabIndex={-1}
          onKeyDown={onMenuKey}
          style={{ top: at.top / at.scale, right: at.right / at.scale, maxHeight: at.maxHeight == null ? undefined : at.maxHeight / at.scale }}
          className="fixed z-50 flex min-w-[180px] flex-col overflow-y-auto rounded-[9px] bg-raised p-1 text-left outline-none shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))]"
        >
          {items.map((item) => {
            const noteId = `${id}-${item.label}`;
            return (
              <div key={item.label} role="none">
                {item.divided && <div role="separator" className="mx-1.5 my-1 h-px bg-hair" />}
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  aria-disabled={item.disabled || undefined}
                  aria-describedby={item.note != null ? noteId : undefined}
                  onClick={() => choose(item)}
                  className={`flex min-h-[32px] w-full items-center rounded-button px-2.5 py-1.5 text-left font-sans text-[13px] leading-[1.25] outline-none focus:bg-hover aria-disabled:cursor-default aria-disabled:opacity-60 ${
                    item.danger ? "text-bad" : "text-ink"
                  } ${item.disabled ? "" : "hover:bg-hover"}`}
                >
                  {item.label}
                </button>
                {item.note != null && (
                  <p id={noteId} aria-hidden="true" className="m-0 px-2.5 pb-1.5 font-sans text-[12px] leading-[1.3] text-ink-3">
                    {item.note}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
