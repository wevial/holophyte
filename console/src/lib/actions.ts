import { addressOf } from "./hosts";
import type { Fetch } from "./poll";
import { withToken } from "./token";

/** The `POST /actions/...` routes the console's buttons post to, by the
 *  label `lib/attention.ts` draws; a label absent here is drawn disabled
 *  as "not wired yet" until its own ticket lands a daemon route. */
export const ROUTES: Record<string, string> = {
  "Restart supervisor": "/actions/restart-supervisor",
  Requeue: "/actions/requeue",
};

/** The title of a button whose daemon has no `[serve] actions = true`. */
export const ACTIONS_OFF = "This daemon has not opted into actions ([serve] actions = true)";
/** The title of a label with no daemon route yet. */
export const NOT_WIRED = "not wired yet";

/** What a `POST /actions/...` answered: the daemon's `ok` and `detail`
 *  (holophyte/serve.py `unit_action()`, `requeue_action()`), or for a
 *  non-2xx answer `ok: false` with its status and `error` as the detail. */
export interface ActionResult {
  ok: boolean;
  detail: string;
}

/** One action: `POST` `route` at `base` with `body` as JSON, the bearer
 *  for `base` added the way every poll carries it (`withToken`). Never
 *  throws: a refused request reads as `ok: false` with the reason, so a
 *  button shows what happened and is enabled again. */
export async function postAction(
  base: string,
  route: string,
  body: Record<string, unknown>,
  fetchImpl: Fetch,
): Promise<ActionResult> {
  const url = `${base}${route}`;
  const init = withToken(addressOf(base), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  let response: Response;
  try {
    response = await fetchImpl(url, init);
  } catch (failure) {
    return { ok: false, detail: failure instanceof Error ? failure.message : String(failure) };
  }
  let answer: unknown = null;
  try {
    answer = await response.json();
  } catch {
    answer = null;
  }
  const record = typeof answer === "object" && answer != null ? (answer as Record<string, unknown>) : {};
  if (!response.ok) {
    const error = typeof record.error === "string" ? record.error : "";
    return { ok: false, detail: `${url} answered ${response.status}${error ? `: ${error}` : ""}` };
  }
  return {
    ok: record.ok === true,
    detail: typeof record.detail === "string" ? record.detail : record.ok === true ? "done" : "the daemon gave no detail",
  };
}
