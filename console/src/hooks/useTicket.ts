import { useEffect, useRef, useState } from "react";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { fetchTicket, type TicketAnswer, type TicketBody } from "../lib/ticket";

export interface TicketState {
  /** `loading` until the first answer for this host and identifier lands. */
  state: TicketAnswer["state"] | "loading";
  /** The mirrored ticket when `state` is `ok`, else null. */
  ticket: TicketBody | null;
  /** The message behind an `error` state, else null. */
  error: string | null;
}

/**
 * The daemon's `/tickets/ID` for the open sheet: fetched once when the
 * pair is set, not per poll, so the body under the operator's eyes does
 * not move while they read. A null `id` fetches nothing.
 */
export function useTicket(base: string, id: string | null, deps: { fetch: Fetch } = defaultPollDeps): TicketState {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const [answer, setAnswer] = useState<{ key: string; answer: TicketAnswer } | null>(null);
  const key = id == null ? null : `${base}#${id}`;

  useEffect(() => {
    if (id == null) return;
    let alive = true;
    void fetchTicket(base, id, fetchRef.current).then((result) => {
      if (alive) setAnswer({ key: `${base}#${id}`, answer: result });
    });
    return () => {
      alive = false;
    };
  }, [base, id]);

  if (key == null || answer == null || answer.key !== key) return { state: "loading", ticket: null, error: null };
  const { answer: result } = answer;
  return {
    state: result.state,
    ticket: result.state === "ok" ? result.ticket : null,
    error: result.state === "error" ? result.error : null,
  };
}
