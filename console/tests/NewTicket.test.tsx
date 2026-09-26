import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NewTicket } from "../src/components/NewTicket";
import { fileTicket } from "../src/lib/boardWrites";
import type { Fetch } from "../src/lib/poll";
import { forgetToken, storeToken } from "../src/lib/token";
import { settle } from "./harness";

const BASE = "http://writer:7710/projects/nat";
const ADDRESS = "writer:7710";
const TEMPLATE = await Bun.file(new URL("../../ticketTemplate.md", import.meta.url)).text();
const host = { base: BASE, project: "/srv/dev/nat" };

afterEach(() => {
  cleanup();
  forgetToken(ADDRESS);
});

/** A fetch that records every request and answers each with `answer`. */
function recording(answer: () => Response) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fetch: Fetch = async (url, init) => {
    calls.push({ url, init });
    return answer();
  };
  return { calls, fetch };
}

test("the form starts as ticketTemplate.md; filing it at priority high posts once with the bearer, and a 422 lists both problems under the kept text", async () => {
  storeToken(ADDRESS, "machine-token");
  const problems = ["Summary is still the template's placeholder", "Acceptance criteria has no checkbox"];
  const { calls, fetch } = recording(() => Response.json({ problems }, { status: 422 }));
  let closed = false;
  render(<NewTicket host={host} onClose={() => (closed = true)} deps={{ fetch }} />);
  const box = screen.getByRole("textbox", { name: "Ticket body" }) as HTMLTextAreaElement;
  expect(box.value).toBe(TEMPLATE);
  const select = screen.getByRole("combobox", { name: "Priority" }) as HTMLSelectElement;
  expect(Array.from(select.options).map((option) => option.textContent)).toEqual(["none", "urgent", "high", "medium", "low"]);
  fireEvent.change(select, { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "File ticket" }));
  await act(settle);

  expect(calls.length).toBe(1);
  const [call] = calls;
  expect(call!.url).toBe(`${BASE}/tickets`);
  expect(call!.init?.method).toBe("POST");
  expect(JSON.parse(call!.init!.body as string)).toEqual({ body: TEMPLATE, priority: 2 });
  expect(new Headers(call!.init!.headers).get("authorization")).toBe("Bearer machine-token");

  const listed = document.querySelector("[data-problems]")!;
  expect(Array.from(listed.querySelectorAll("li")).map((item) => item.textContent)).toEqual(problems);
  expect(box.compareDocumentPosition(listed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(box.value).toBe(TEMPLATE);
  expect(closed).toBe(false);
});

test("a 201 closes the form", async () => {
  const { fetch } = recording(() => Response.json({ ticket: "NAT-3", revision: 1 }, { status: 201 }));
  let closed = false;
  render(<NewTicket host={host} onClose={() => (closed = true)} deps={{ fetch }} />);
  fireEvent.click(screen.getByRole("button", { name: "File ticket" }));
  await act(settle);
  expect(closed).toBe(true);
});

test("fileTicket() answers the ticket on 201, the problems on 422 and an error otherwise, never throwing", async () => {
  const answer = (response: Response | Error): Fetch => async () => {
    if (response instanceof Error) throw response;
    return response;
  };
  expect(await fileTicket(BASE, "x", null, answer(Response.json({ ticket: "NAT-4", revision: 1 }, { status: 201 })))).toEqual({ ok: true, ticket: "NAT-4" });
  expect(await fileTicket(BASE, "x", null, answer(Response.json({ problems: ["one"] }, { status: 422 })))).toEqual({ ok: false, problems: ["one"] });
  expect(await fileTicket(BASE, "x", null, answer(Response.json({}, { status: 401 })))).toEqual({ ok: false, error: `${BASE}/tickets answered 401` });
  expect(await fileTicket(BASE, "x", null, answer(new Error("unreachable")))).toEqual({ ok: false, error: "unreachable" });
});
