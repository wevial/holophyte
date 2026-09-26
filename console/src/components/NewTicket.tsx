import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import TEMPLATE from "../../../ticketTemplate.md" with { type: "text" };
import { fileTicket } from "../lib/boardWrites";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";
import { defaultPollDeps, type Fetch } from "../lib/poll";

/** The priority select's choices, as the board numbers them. */
export const PRIORITIES: { label: string; value: number | null }[] = [
  { label: "none", value: null },
  { label: "urgent", value: 1 },
  { label: "high", value: 2 },
  { label: "medium", value: 3 },
  { label: "low", value: 4 },
];

/**
 * The New ticket form for one editable host: a fixed panel on the right
 * edge like the ticket sheet, its textarea starting as `ticketTemplate.md`
 * and a priority select beneath. File posts the text through
 * `fileTicket()`; a 422 lists every problem under the textarea and keeps
 * the text, any other failure shows its reason, and a filing closes the
 * form, the next poll showing the card. Escape, the backdrop or Cancel
 * calls `onClose`.
 */
export function NewTicket({
  host,
  onClose,
  deps = defaultPollDeps,
}: {
  host: Pick<HostRecord, "base" | "project">;
  onClose: () => void;
  deps?: { fetch: Fetch };
}) {
  const [text, setText] = useState(TEMPLATE);
  const [priority, setPriority] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const titleId = useId();

  useEffect(() => {
    box.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    const result = await fileTicket(host.base, text, priority === "" ? null : Number(priority), deps.fetch);
    setPending(false);
    if (result.ok) return onClose();
    setProblems("problems" in result ? result.problems : []);
    setError("error" in result ? result.error : null);
  };

  return (
    <div data-new-ticket className="fixed inset-0 z-40">
      <div data-backdrop aria-hidden="true" onClick={onClose} className="absolute inset-0 bg-ink/40" />
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onSubmit={submit}
        className="absolute inset-y-0 right-0 flex w-[560px] max-w-full flex-col gap-3 overflow-y-auto border-l border-line bg-card px-5 py-4 shadow-card"
      >
        <header className="flex items-center gap-2">
          <h2 id={titleId} className="text-[15px] font-semibold text-ink">
            New ticket
          </h2>
          {host.project != null && <span className="font-mono text-[12px] text-muted">{projectName(host.project)}</span>}
        </header>
        <textarea
          ref={box}
          aria-label="Ticket body"
          value={text}
          onChange={(event) => setText(event.target.value)}
          spellCheck={false}
          className="min-h-[360px] flex-1 rounded-button border border-chip-border bg-well p-3 font-mono text-[12px] leading-[1.5] text-body"
        />
        {problems.length > 0 && (
          <ul data-problems role="alert" className="flex list-disc flex-col gap-1 pl-5 font-mono text-[11px] text-bad-text">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        )}
        {error != null && (
          <p role="alert" className="font-mono text-[11px] text-bad-text">
            filing failed: {error}
          </p>
        )}
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-[12px] text-muted">
            Priority
            <select
              value={priority}
              onChange={(event) => setPriority(event.target.value)}
              className="rounded-button border border-chip-border bg-card px-2 py-[2px] text-[12px] text-ink"
            >
              {PRIORITIES.map((choice) => (
                <option key={choice.label} value={choice.value ?? ""}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded-button border border-chip-border px-3 py-1 text-[12px] font-semibold text-ink"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={pending}
            className="rounded-button bg-accent px-3 py-1 text-[12px] font-semibold text-card disabled:opacity-60"
          >
            File ticket
          </button>
        </div>
      </form>
    </div>
  );
}
