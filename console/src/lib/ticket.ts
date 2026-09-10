import type { Fetch } from "./poll";
import type { BoardState } from "./types";

/** The daemon's `GET /tickets/KO-n` body (`docs/reference/http.md`). */
export interface TicketBody {
  ticket: string;
  title: string | null;
  status: BoardState | "merged" | "abandoned";
  body: string;
  acceptance_criteria: string[];
  verification_commands: string[];
  time_box_ms: number | null;
  run: number | null;
  mirrored_ms: number | null;
}

/** The three outcomes the sheet tells apart: the mirrored ticket, the
 *  daemon asking for its token (401), or no such ticket mirrored on this
 *  host (404). Any other failure is `error` with its message. */
export type TicketAnswer =
  | { state: "ok"; ticket: TicketBody }
  | { state: "needs_token" }
  | { state: "not_mirrored" }
  | { state: "error"; error: string };

/** One `/tickets/ID` from the daemon at `base`; never throws. The token
 *  for the host's address rides in through the page's fetch seam
 *  (`tokenedFetch`), the same way every other JSON request carries it. */
export async function fetchTicket(base: string, id: string, fetchImpl: Fetch): Promise<TicketAnswer> {
  const url = `${base}/tickets/${encodeURIComponent(id)}`;
  try {
    const response = await fetchImpl(url, { headers: { accept: "application/json" } });
    if (response.status === 401) return { state: "needs_token" };
    if (response.status === 404) return { state: "not_mirrored" };
    if (!response.ok) return { state: "error", error: `${url} answered ${response.status}` };
    return { state: "ok", ticket: (await response.json()) as TicketBody };
  } catch (failure) {
    return { state: "error", error: failure instanceof Error ? failure.message : String(failure) };
  }
}
