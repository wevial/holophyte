import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";

const SEARCH_DEBOUNCE_MS = 200;

type PageSearch = Record<string, unknown>;

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** A list page's search: its debounced, trimmed text is the address's
 *  `q`, and `/` anywhere on the page focuses it. */
export function SearchField({ label }: { label: string }) {
  const { q } = useSearch({ strict: false }) as { q?: string };
  const navigate = useNavigate();
  const [text, setText] = useState(q ?? "");
  const written = useRef(q);
  const field = useRef<HTMLInputElement>(null);
  const write = (next: string | undefined) => {
    written.current = next;
    void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, q: next }), replace: true });
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
      aria-label={label}
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

interface FilterOption {
  value: string;
  label: string;
}

/** `all` under `allLabel`, then each of `options`, as the address's `param`;
 *  `all` drops it. */
export function FilterSelect({ label, param, allLabel, options }: { label: string; param: string; allLabel: string; options: readonly FilterOption[] }) {
  const search = useSearch({ strict: false }) as PageSearch;
  const navigate = useNavigate();
  const value = search[param];
  return (
    <select
      aria-label={label}
      value={typeof value === "string" ? value : "all"}
      onChange={(event) => {
        const picked = options.find((each) => each.value === event.target.value)?.value;
        void navigate({ to: ".", search: (previous: PageSearch) => ({ ...previous, [param]: picked }) });
      }}
      className="min-h-8 cursor-pointer border border-rule bg-paper px-2.5 font-sans text-[12.5px] text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
    >
      <option value="all">{allLabel}</option>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}
