import type { Fetch } from "../src/lib/poll";

interface Seen {
  url: string;
  method: string | undefined;
  authorization: string | null;
  body: unknown;
}

/** A `fetch` that records each request and answers every one with `reply`
 *  (a body, or a Response for a non-2xx answer); `gate` holds the answer
 *  until the test releases it. */
export function fakeFetch(reply: Record<string, unknown> | (() => Response), gate?: Promise<void>) {
  const seen: Seen[] = [];
  const fetchImpl: Fetch = async (url, init) => {
    seen.push({
      url,
      method: init?.method,
      authorization: new Headers(init?.headers).get("authorization"),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : init?.body,
    });
    if (gate) await gate;
    return typeof reply === "function" ? reply() : Response.json(reply);
  };
  return { seen, fetchImpl };
}

