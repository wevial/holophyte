const FOCUSABLE = "button:not(:disabled), a[href], textarea, input, select, video[controls], [tabindex]:not([tabindex='-1'])";

/** Esc cancels before any page listener sees it; Tab and Shift+Tab cycle
 *  through `box`'s controls, from wherever focus is on the page. Into and
 *  through a video the browser moves focus, so its inner controls are
 *  reached; focus that then leaves `box` wraps to its other end. */
export function trap(event: KeyboardEvent, box: HTMLElement | null, onCancel: () => void) {
  if (event.key === "Escape") {
    event.preventDefault();
    onCancel();
    return;
  }
  if (event.key !== "Tab" || box == null) return;
  const stops = [...box.querySelectorAll<HTMLElement>(FOCUSABLE)];
  if (stops.length === 0) return;
  const active = document.activeElement as HTMLElement;
  const at = stops.indexOf(active);
  const wraps = event.shiftKey ? at <= 0 : at < 0 || at === stops.length - 1;
  const next = event.shiftKey ? (wraps ? stops.length - 1 : at - 1) : wraps ? 0 : at + 1;
  const video = (at >= 0 && active instanceof HTMLVideoElement) || (!wraps && stops[next] instanceof HTMLVideoElement);
  if (video) {
    const other = event.shiftKey ? stops.at(-1)! : stops[0]!;
    setTimeout(() => {
      if (!box.contains(document.activeElement)) other.focus();
    });
    return;
  }
  event.preventDefault();
  stops[next]!.focus();
}
