import { addressOf } from "./hosts";
import type { Fetch } from "./poll";
import { withToken } from "./token";

/** What a filing answered: the new ticket on a 201, every blocking
 *  problem on a 422 (holophyte/serve_board.py `on_store()`), or for any
 *  other answer or a failed request the reason as `error`. */
export type FileResult = { ok: true; ticket: string } | { ok: false; problems: string[] } | { ok: false; error: string };

/** File a new ticket: `POST` `{"body", "priority"}` to `base`'s
 *  `/tickets`, the bearer for `base` added the way every poll carries it
 *  (`withToken`). `priority` is 0 to 4, null for none. Never throws: a
 *  refused request reads as `problems` or `error`, so the form keeps its
 *  text and says why. */
export async function fileTicket(base: string, body: string, priority: number | null, fetchImpl: Fetch): Promise<FileResult> {
  const url = `${base}/tickets`;
  const init = withToken(addressOf(base), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ body, priority }),
  });
  let response: Response;
  try {
    response = await fetchImpl(url, init);
  } catch (failure) {
    return { ok: false, error: failure instanceof Error ? failure.message : String(failure) };
  }
  let answer: unknown = null;
  try {
    answer = await response.json();
  } catch {
    answer = null;
  }
  const record = typeof answer === "object" && answer != null ? (answer as Record<string, unknown>) : {};
  if (response.status === 201 && typeof record.ticket === "string") return { ok: true, ticket: record.ticket };
  if (response.status === 422 && Array.isArray(record.problems)) return { ok: false, problems: record.problems.map(String) };
  const error = typeof record.error === "string" ? record.error : "";
  return { ok: false, error: `${url} answered ${response.status}${error ? `: ${error}` : ""}` };
}
