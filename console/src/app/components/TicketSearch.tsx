import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { FILTER_LABELS, STATE_FILTERS, type TicketsSearch } from "../tickets";

const SEARCH_DEBOUNCE_MS = 200;

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** The Tickets page's search: its debounced, trimmed text is the address's
 *  `q`, and `/` anywhere on the page focuses it. */
export function TicketSearchField() {
  const { q }: TicketsSearch = useSearch({ from: "/tickets" });
  const navigate = useNavigate();
  const [text, setText] = useState(q ?? "");
  const written = useRef(q);
  const field = useRef<HTMLInputElement>(null);
  const write = (next: string | undefined) => {
    written.current = next;
    void navigate({ to: ".", search: (previous: TicketsSearch) => ({ ...previous, q: next }), replace: true });
  };
  useEffect(() => {
    if (q === written.current) return;
    written.current = q;
    setText(q ?? "");
  }, [q]);
  useEffect(() => {
    const next = text.trim() === "" ? undefined : text.trim();
    if (next === written.current) return;
    const timer = setTimeout(() => write(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text]);
  useEffect(() => {
    const focus = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || typing(event.target)) return;
      event.preventDefault();
      field.current?.focus();
    };
    document.addEventListener("keydown", focus);
    return () => document.removeEventListener("keydown", focus);
  }, []);
  return (
    <input
      ref={field}
      type="search"
      aria-label="Search tickets"
      placeholder="Search"
      value={text}
      onChange={(event) => setText(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        setText("");
        write(undefined);
      }}
      className="min-h-8 w-56 border border-rule bg-paper px-3 font-sans text-[12.5px] text-ink placeholder:text-ink-3 focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
    />
  );
}

/** All states, then each state of `STATE_FILTERS`, as the address's `filter`. */
export function StatePicker() {
  const { filter }: TicketsSearch = useSearch({ from: "/tickets" });
  const navigate = useNavigate();
  return (
    <select
      aria-label="State"
      value={filter ?? "all"}
      onChange={(event) => {
        const picked = event.target.value;
        const state = STATE_FILTERS.find((each) => each === picked);
        void navigate({ to: ".", search: (previous: TicketsSearch) => ({ ...previous, filter: state }) });
      }}
      className="min-h-8 cursor-pointer border border-rule bg-paper px-2.5 font-sans text-[12.5px] text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
    >
      <option value="all">All states</option>
      {STATE_FILTERS.map((state) => (
        <option key={state} value={state}>
          {FILTER_LABELS[state]}
        </option>
      ))}
    </select>
  );
}
