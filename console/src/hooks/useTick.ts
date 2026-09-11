import { useEffect, useReducer } from "react";

/** The page's second hand: re-render the caller every `ms` on one
 *  `setInterval`, cleared on unmount. Returns the local clock at render
 *  time, the `now` a drawing site ages the daemon's frozen numbers with
 *  between polls. */
export function useTick(ms: number): number {
  const [, bump] = useReducer((count: number) => count + 1, 0);
  useEffect(() => {
    const id = setInterval(bump, ms);
    return () => clearInterval(id);
  }, [ms]);
  return Date.now();
}
