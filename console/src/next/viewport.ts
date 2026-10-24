import { useSyncExternalStore } from "react";

const WIDE_QUERY = "(min-width: 1400px)";

function wide(): boolean {
  return window.matchMedia?.(WIDE_QUERY).matches ?? false;
}

function subscribeWide(listener: () => void) {
  const query = window.matchMedia?.(WIDE_QUERY);
  query?.addEventListener("change", listener);
  return () => query?.removeEventListener("change", listener);
}

/** Whether the window is at least 1400px wide. */
export function useWide(): boolean {
  return useSyncExternalStore(subscribeWide, wide);
}
