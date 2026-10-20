const FOCUSABLE = "button:not(:disabled), a[href], textarea, input, select, video[controls], [tabindex]:not([tabindex='-1'])";

/** Esc cancels before any page listener sees it; Tab and Shift+Tab cycle
 *  through `box`'s controls, from wherever focus is on the page. */
export function trap(event: KeyboardEvent, box: HTMLElement | null, onCancel: () => void) {
  if (event.key === "Escape") {
    event.preventDefault();
    onCancel();
    return;
  }
  if (event.key !== "Tab" || box == null) return;
  const stops = [...box.querySelectorAll<HTMLElement>(FOCUSABLE)];
  if (stops.length === 0) return;
  const at = stops.indexOf(document.activeElement as HTMLElement);
  const next = event.shiftKey ? (at <= 0 ? stops.length - 1 : at - 1) : at < 0 || at === stops.length - 1 ? 0 : at + 1;
  event.preventDefault();
  stops[next]!.focus();
}
