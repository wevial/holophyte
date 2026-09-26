import { addressOf } from "./hosts";
import type { Fetch } from "./poll";
import { withToken } from "./token";

/** What a filing answered: the new ticket on a 201, every blocking
 *  problem on a 422 (holophyte/serve_board.py `on_store()`), or for any
 *  other answer or a failed request the reason as `error`. */
export type FileResult = { ok: true; ticket: string } | { ok: false; problems: string[] } | { ok: false; error: string };

/** What a revision-checked edit, move or cancel answered: the ticket's new
 *  `revision` on a 200; on a 409 the revision it is at `current`, another
 *  write having landed since it was read; every blocking problem on a
 *  422; or for any other answer or a failed request the reason as `error`. */
export type WriteResult =
  | { ok: true; revision: number }
  | { ok: false; conflict: true; current: number | null }
  | { ok: false; problems: string[] }
  | { ok: false; error: string };

/** One JSON write to `url` with the bearer for `base` (`withToken`) and
 *  any extra `headers`: its status and body as a record, or the reason the
 *  request failed. Never throws. */
async function send(
  base: string,
  url: string,
  method: string,
  payload: unknown,
  fetchImpl: Fetch,
  headers: Record<string, string> = {},
): Promise<{ status: number; record: Record<string, unknown> } | { error: string }> {
  const init = withToken(addressOf(base), {
    method,
    headers: { "content-type": "application/json", accept: "application/json", ...headers },
    body: JSON.stringify(payload),
  });
  let response: Response;
  try {
    response = await fetchImpl(url, init);
  } catch (failure) {
    return { error: failure instanceof Error ? failure.message : String(failure) };
  }
  let answer: unknown = null;
  try {
    answer = await response.json();
  } catch {
    answer = null;
  }
  return { status: response.status, record: typeof answer === "object" && answer != null ? (answer as Record<string, unknown>) : {} };
}

const refusal = (url: string, status: number, record: Record<string, unknown>) => {
  const error = typeof record.error === "string" ? record.error : "";
  return `${url} answered ${status}${error ? `: ${error}` : ""}`;
};

/** File a new ticket: `POST` `{"body", "priority"}` to `base`'s
 *  `/tickets`, the bearer for `base` added the way every poll carries it
 *  (`withToken`). `priority` is 0 to 4, null for none. Never throws: a
 *  refused request reads as `problems` or `error`, so the form keeps its
 *  text and says why. */
export async function fileTicket(base: string, body: string, priority: number | null, fetchImpl: Fetch): Promise<FileResult> {
  const url = `${base}/tickets`;
  const sent = await send(base, url, "POST", { body, priority }, fetchImpl);
  if ("error" in sent) return { ok: false, error: sent.error };
  const { status, record } = sent;
  if (status === 201 && typeof record.ticket === "string") return { ok: true, ticket: record.ticket };
  if (status === 422 && Array.isArray(record.problems)) return { ok: false, problems: record.problems.map(String) };
  return { ok: false, error: refusal(url, status, record) };
}

/** One revision-checked write: `payload` to `base`'s `/tickets/ID` plus
 *  `suffix` with `If-Match: revision`. */
async function revised(
  base: string,
  id: string,
  suffix: string,
  method: string,
  revision: number,
  payload: unknown,
  fetchImpl: Fetch,
): Promise<WriteResult> {
  const url = `${base}/tickets/${encodeURIComponent(id)}${suffix}`;
  const sent = await send(base, url, method, payload, fetchImpl, { "if-match": String(revision) });
  if ("error" in sent) return { ok: false, error: sent.error };
  const { status, record } = sent;
  if (status === 200 && typeof record.revision === "number") return { ok: true, revision: record.revision };
  if (status === 409) return { ok: false, conflict: true, current: typeof record.current === "number" ? record.current : null };
  if (status === 422 && Array.isArray(record.problems)) return { ok: false, problems: record.problems.map(String) };
  return { ok: false, error: refusal(url, status, record) };
}

/** Save `body` as ticket `id`'s full text: `PUT /tickets/ID` at the
 *  `revision` the sheet read. Never throws. */
export function editTicket(base: string, id: string, revision: number, body: string, fetchImpl: Fetch): Promise<WriteResult> {
  return revised(base, id, "", "PUT", revision, { body }, fetchImpl);
}

/** Move ticket `id` to `column`, `ready` or `backlog`:
 *  `POST /tickets/ID/move` at the `revision` the sheet read. Never throws. */
export function moveTicket(base: string, id: string, revision: number, column: "ready" | "backlog", fetchImpl: Fetch): Promise<WriteResult> {
  return revised(base, id, "/move", "POST", revision, { column }, fetchImpl);
}

/** Cancel ticket `id` with the reason `note`, ending its live run:
 *  `POST /tickets/ID/cancel` at the `revision` the sheet read. Never throws. */
export function cancelTicket(base: string, id: string, revision: number, note: string, fetchImpl: Fetch): Promise<WriteResult> {
  return revised(base, id, "/cancel", "POST", revision, { note }, fetchImpl);
}
