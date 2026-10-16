type ConsoleView = "classic" | "next";

export const VIEW_KEY = "holophyte.view";

const PAGES: Record<ConsoleView, string> = { classic: "/", next: "/next.html" };

/** The remembered front end, `classic` when nothing valid is stored or
 *  storage is unavailable. */
function readView(): ConsoleView {
  try {
    return localStorage.getItem(VIEW_KEY) === "next" ? "next" : "classic";
  } catch {
    return "classic";
  }
}

export function writeView(view: ConsoleView): void {
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    // Storage may be disabled; the switch still happens for this visit.
  }
}

/** Remember `view` and load its page, carrying `hash` along. */
export function switchView(view: ConsoleView, hash: string): void {
  writeView(view);
  location.assign(PAGES[view] + hash);
}

/** A link into the classic view at `hash` that opens it whatever view is
 *  remembered, without changing the remembered choice. */
export function classicHref(hash = ""): string {
  return `${PAGES.classic}?classic${hash}`;
}

/** At the classic entry: replace the page with the new view when it was
 *  chosen last, unless the address asks for `?classic`. True when the
 *  page is being replaced and should not render. */
export function openRememberedView(): boolean {
  if (readView() !== "next" || new URLSearchParams(location.search).has("classic")) return false;
  location.replace(PAGES.next + location.hash);
  return true;
}
