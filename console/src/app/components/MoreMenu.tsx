import { Fragment, useEffect, useId, useRef, useState } from "react";
import { Menu, MenuContent, MenuItem as SdkMenuItem, MenuSeparator, MenuTrigger } from "@ui-sdk/react/menu";

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

/** A ⋯ button named `label` opening a menu of `items`, placed under the
 *  button and kept inside the viewport, scrolling when taller.
 *  `onOpenChange` hears it open and close. Arrow keys, Home, End and
 *  typeahead move between items; Esc or a choice closes it and returns
 *  focus to the button, and page scroll or window resize closes it. Its
 *  clicks never reach the row it sits in; `compact` is a row's smaller
 *  unedged button. */
export function MoreMenu({ label, items, className = "", compact = false, onOpenChange }: {
  label: string; items: MenuItem[]; className?: string; compact?: boolean; onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const content = useRef<HTMLDivElement>(null);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;

  const setMenuOpen = (next: boolean) => {
    setOpen(next);
    onOpenChangeRef.current?.(next);
  };

  useEffect(() => {
    if (!open) return;
    const close = () => setMenuOpen(false);
    const scrolled = (event: Event) => {
      if (event.target instanceof Node && content.current?.contains(event.target)) return;
      close();
    };
    window.addEventListener("scroll", scrolled, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", scrolled, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  return (
    <div className={className} onClick={(event) => event.stopPropagation()}>
      <Menu open={open} onOpenChange={setMenuOpen} modal={false}>
        <MenuTrigger
          aria-label={label}
          className={`${TRIGGER} ${compact ? "size-[26px] aria-expanded:bg-[color-mix(in_oklab,var(--accent)_32%,var(--selected))]" : "size-[34px] shadow-[inset_0_0_0_1px_var(--hair-2)]"}`}
        >
          <DotsIcon />
        </MenuTrigger>
        <MenuContent
          ref={content}
          side="bottom"
          align="end"
          sideOffset={compact ? 3 : 6}
          collisionPadding={8}
          onKeyDown={(event) => {
            // Ignore repeated Enter/Space so holding the key that opened the menu chooses nothing.
            if (event.repeat && (event.key === "Enter" || event.key === " ")) event.preventDefault();
          }}
          style={{ maxHeight: "var(--available-height)" }}
          className="z-50 flex min-w-[180px] flex-col overflow-y-auto rounded-[9px] border-0 bg-raised p-1 text-left text-ink outline-none shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))]"
        >
          {items.map((item, index) => {
            const noteId = `${id}-note-${index}`;
            return (
              <Fragment key={item.label}>
                {item.divided && <MenuSeparator className="mx-0.5 my-1 h-px bg-hair" />}
                <SdkMenuItem
                  label={item.label}
                  disabled={item.disabled}
                  destructive={item.danger}
                  aria-describedby={item.note != null ? noteId : undefined}
                  onSelect={() => {
                    item.onSelect();
                  }}
                  className={`flex min-h-[32px] w-full cursor-pointer items-center rounded-button px-2.5 py-1.5 text-left font-sans text-[13px] leading-[1.25] outline-none focus:bg-hover data-highlighted:bg-hover aria-disabled:cursor-default aria-disabled:opacity-60 ${
                    item.danger ? "text-bad" : "text-ink"
                  }`}
                >
                  {item.label}
                </SdkMenuItem>
                {item.note != null && (
                  <p id={noteId} aria-hidden="true" className="m-0 px-2.5 pb-1.5 font-sans text-[12px] leading-[1.3] text-ink-3">
                    {item.note}
                  </p>
                )}
              </Fragment>
            );
          })}
        </MenuContent>
      </Menu>
    </div>
  );
}
