import { useCallback, useSyncExternalStore } from "react";

function useMinWidth(px: number): boolean {
  const media = `(min-width: ${px}px)`;
  const subscribe = useCallback((listener: () => void) => {
    const query = window.matchMedia?.(media);
    query?.addEventListener("change", listener);
    return () => query?.removeEventListener("change", listener);
  }, [media]);
  return useSyncExternalStore(subscribe, () => window.matchMedia?.(media).matches ?? false);
}

/** Whether the window is at least 1100px wide, room for a table's column head. */
export function useTableWidth(): boolean {
  return useMinWidth(1100);
}

/** Whether the window is at least 1440px wide, room for the scout page's chat column. */
export function useChatWidth(): boolean {
  return useMinWidth(1440);
}

/** Whether the window is at least 1100px wide, room for the scout list beside a scout. */
export function useScoutListWidth(): boolean {
  return useMinWidth(1100);
}
