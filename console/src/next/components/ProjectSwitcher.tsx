import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { matchRows, storeScope, type SwitcherRow } from "../projects";
import { AllProjectsIcon, DropIcon, FolderIcon } from "./icons";

const DIALOG_WIDTH = 264;
const EDGE = 8;
const LEAST_HEIGHT = 164;

function iconColour(row: SwitcherRow): string {
  if (row.name == null) return "text-ink-2";
  if (row.down) return "text-bad";
  if (row.held) return "text-wait";
  return "text-live";
}

function RowIcon({ row }: { row: SwitcherRow }) {
  return <span className={`flex ${iconColour(row)}`}>{row.name == null ? <AllProjectsIcon /> : <FolderIcon />}</span>;
}

function HeldTag() {
  return <span className="font-mono text-[11px] font-normal text-wait">held</span>;
}

function buttonName(row: SwitcherRow): string {
  const facts = [row.label];
  if (row.held) facts.push("held");
  if (row.down) facts.push("not answering");
  if (row.need > 0) facts.push(`${row.need} need you`);
  return `Project: ${facts.join(", ")}. Switch project`;
}

function unknownRow(name: string): SwitcherRow {
  return { name, label: name, held: false, need: 0, open: 0, down: false };
}

export function ProjectSwitcher({ rows, folded }: { rows: SwitcherRow[]; folded: boolean }) {
  const { project } = useSearch({ from: "__root__" });
  const navigate = useNavigate();
  const id = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<{ top: number; left: number; maxHeight: number } | null>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  const current = rows.find((row) => row.name === (project ?? null)) ?? unknownRow(project!);
  const matches = matchRows(rows, query);
  const at = Math.min(active, Math.max(0, matches.length - 1));
  const open = place != null;

  useEffect(() => {
    if (!open) return;
    const outside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!dialogRef.current?.contains(target) && !buttonRef.current?.contains(target)) setPlace(null);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  const show = () => {
    const rect = buttonRef.current!.getBoundingClientRect();
    const rail = buttonRef.current!.closest("aside")?.getBoundingClientRect() ?? rect;
    const room = window.innerWidth - DIALOG_WIDTH - EDGE;
    const beside = folded && rail.right + 4 <= room;
    const below = beside ? rect.top : rect.bottom + 4;
    const top = Math.max(EDGE, Math.min(below, window.innerHeight - EDGE - LEAST_HEIGHT));
    const left = beside ? rail.right + 4 : Math.max(EDGE, Math.min(rect.left, room));
    setPlace({ top, left, maxHeight: window.innerHeight - top - EDGE });
    setQuery("");
    setActive(Math.max(0, rows.indexOf(current)));
  };

  useEffect(() => {
    if (open) document.getElementById(`${id}-option-${at}`)?.scrollIntoView?.({ block: "nearest" });
  }, [open, at, id]);

  const close = () => {
    setPlace(null);
    buttonRef.current?.focus();
  };

  const pick = (row: SwitcherRow) => {
    storeScope(row.name);
    void navigate({ to: ".", search: (previous) => ({ ...previous, project: row.name ?? undefined }) });
    close();
  };

  const onButtonKey = (event: KeyboardEvent) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    if (!open) show();
  };

  const onFindKey = (event: KeyboardEvent) => {
    const count = matches.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (count > 0) setActive((at + (event.key === "ArrowDown" ? 1 : count - 1)) % count);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const row = matches[at];
      if (row) pick(row);
    } else if (event.key === "Tab") {
      setPlace(null);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  };

  const optionId = (index: number) => `${id}-option-${index}`;
  const listId = `${id}-list`;
  const findId = `${id}-find`;

  return (
    <div className={`pt-3 pb-1 ${folded ? "flex justify-center" : "px-3"}`}>
      <button
        ref={buttonRef}
        type="button"
        aria-label={buttonName(current)}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={folded ? current.label : undefined}
        onClick={() => (open ? setPlace(null) : show())}
        onKeyDown={onButtonKey}
        className={`relative flex min-h-11 items-center gap-2 border border-rule bg-paper text-[13px] text-ink hover:bg-paper-2 ${
          folded ? "size-11 justify-center" : "w-full px-3"
        }`}
      >
        <RowIcon row={current} />
        {folded ? (
          current.need > 0 && (
            <span data-need aria-hidden="true" className="absolute top-0.5 right-1 font-mono text-[9px] leading-none font-semibold text-need">
              {current.need}
            </span>
          )
        ) : (
          <>
            <span className="min-w-0 flex-1 truncate text-left font-medium">{current.label}</span>
            {current.held && <HeldTag />}
            {current.need > 0 && (
              <span data-need aria-hidden="true" className="font-mono text-[12px] font-semibold text-need">
                {current.need}
              </span>
            )}
            <span className="text-ink-3">
              <DropIcon />
            </span>
          </>
        )}
      </button>

      {place && (
        <div
          ref={dialogRef}
          role="dialog"
          aria-label="Switch project"
          style={{ top: place.top, left: place.left, maxHeight: place.maxHeight, width: `min(${DIALOG_WIDTH}px, calc(100vw - ${2 * EDGE}px))` }}
          className="fixed z-50 flex flex-col border border-rule bg-paper p-2 shadow-card"
          onMouseDown={(event) => {
            if (event.target !== event.currentTarget.querySelector("input")) event.preventDefault();
          }}
          onBlur={(event) => {
            const next = event.relatedTarget as Node | null;
            if (!dialogRef.current?.contains(next) && next !== buttonRef.current) setPlace(null);
          }}
        >
          <label htmlFor={findId} className="sr-only">
            Find a project
          </label>
          <input
            id={findId}
            autoFocus
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={matches.length > 0 ? optionId(at) : undefined}
            placeholder="Find a project"
            autoComplete="off"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            onKeyDown={onFindKey}
            className="min-h-9 border border-ink-3 bg-paper-2 px-2.5 text-[13px] text-ink placeholder:text-ink-2 focus:outline-2 focus:outline-act focus:-outline-offset-1!"
          />
          {matches.length === 0 ? (
            <p className="mt-2 px-2 py-2.5 text-[13px] text-ink-3">No project matches.</p>
          ) : (
            <ul id={listId} role="listbox" aria-label="Projects" className="mt-2 flex max-h-[320px] min-h-0 flex-col overflow-y-auto">
              {matches.map((row, index) => (
                <li
                  key={row.name == null ? "all-projects-scope" : `project:${row.name}`}
                  id={optionId(index)}
                  role="option"
                  aria-selected={row.name === current.name}
                  data-active={index === at}
                  onClick={() => pick(row)}
                  onMouseMove={() => setActive(index)}
                  className="flex min-h-10 cursor-pointer items-center gap-2 px-2 text-[13px] text-ink aria-selected:text-act data-[active=true]:bg-paper-2"
                >
                  <RowIcon row={row} />
                  <span className="min-w-0 flex-1 truncate">{row.label}</span>
                  {row.held && <HeldTag />}
                  {row.need > 0 && <span className="font-mono text-[12px] font-semibold text-need">{row.need}</span>}
                  <span className="font-mono text-[11px] text-ink-3">{row.down ? "not answering" : `${row.open} open`}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
