import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

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

/** A row's ⋯ menu shows while the row (a `group`) is hovered or focused,
 *  while the menu is open, and always where nothing can hover. */
export const ROW_MENU = "opacity-0 group-hover:opacity-100 focus-within:opacity-100 has-[[aria-expanded=true]]:opacity-100 [@media(hover:none)]:opacity-100";

function DotsIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={16} height={16} fill="currentColor" className="size-4 shrink-0">
      <circle cx="3.5" cy="8" r="1.25" />
      <circle cx="8" cy="8" r="1.25" />
      <circle cx="12.5" cy="8" r="1.25" />
    </svg>
  );
}

/** A ⋯ button named `label` opening a menu of `items`: arrow keys, Home and
 *  End move between them, Esc or a choice closes it and returns focus to
 *  the button. Its clicks never reach the row it sits in. */
export function MoreMenu({ label, items, className = "" }: { label: string; items: MenuItem[]; className?: string }) {
  const [at, setAt] = useState<{ top: number; right: number } | null>(null);
  const [focusFirst, setFocusFirst] = useState(true);
  const id = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const open = at != null;
  const entries = () => [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];

  useEffect(() => {
    if (!open) return;
    const all = entries();
    (focusFirst ? all[0] : all.at(-1))?.focus();
    const outside = (event: Event) => {
      if (!wrap.current?.contains(event.target as Node)) setAt(null);
    };
    const away = () => setAt(null);
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("resize", away);
    window.addEventListener("scroll", away, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("resize", away);
      window.removeEventListener("scroll", away, true);
    };
  }, [open, focusFirst]);

  const show = (first: boolean) => {
    const box = button.current!.getBoundingClientRect();
    setFocusFirst(first);
    setAt({ top: box.bottom + 6, right: Math.max(8, window.innerWidth - box.right) });
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
    show(event.key === "ArrowDown");
  };
  const onMenuKey = (event: KeyboardEvent) => {
    const all = entries();
    const now = all.indexOf(document.activeElement as HTMLElement);
    const step: Record<string, number> = { ArrowDown: now + 1, ArrowUp: now - 1, Home: 0, End: all.length - 1 };
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
        onClick={() => (open ? setAt(null) : show(true))}
        onKeyDown={onButtonKey}
        className="inline-flex size-8 items-center justify-center rounded-button text-ink-2 shadow-[inset_0_0_0_1px_var(--hair-2)] hover:bg-hover hover:text-ink aria-expanded:bg-selected aria-expanded:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      >
        <DotsIcon />
      </button>
      {at != null && (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKey}
          style={{ top: at.top, right: at.right }}
          className="fixed z-50 flex min-w-[180px] flex-col rounded-[9px] bg-raised p-1 text-left shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))]"
        >
          {items.map((item) => {
            const noteId = `${id}-${item.label}`;
            return (
              <div key={item.label} role="none">
                {item.divided && <div role="separator" className="mx-1 my-1 h-px bg-hair-2" />}
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  aria-disabled={item.disabled || undefined}
                  aria-describedby={item.note != null ? noteId : undefined}
                  onClick={() => choose(item)}
                  className={`flex min-h-[30px] w-full items-center rounded-button px-2.5 py-1.5 text-left font-sans text-[13px] leading-[1.25] outline-none focus:bg-hover aria-disabled:cursor-default aria-disabled:opacity-60 ${
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
