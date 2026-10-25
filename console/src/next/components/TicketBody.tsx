import { useContext, useId, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Markdown } from "../../components/Markdown";
import { editTicket } from "../../lib/boardWrites";
import { PollDepsContext } from "../scope";
import { HeaderDate, headerSpan, readHeader, type Table, type Value } from "../ticketHeader";
import { Btn } from "./Btn";
import { useToast } from "./Toast";

export type BodyView = "fields" | "markdown";

const VIEWS: { view: BodyView; label: string }[] = [
  { view: "fields", label: "Fields" },
  { view: "markdown", label: "Markdown" },
];

const RENDERED = [
  "max-w-[75ch] font-read text-[15px] leading-[1.55] text-ink",
  "[&_.ticket-body>*+*]:mt-[0.7em] [&_p]:m-0",
  "[&_h1]:font-sans [&_h2]:font-sans [&_h3]:font-sans [&_h4]:font-sans",
  "[&_h1]:text-[17px] [&_h2]:text-[15px] [&_h3]:text-[14px] [&_h4]:text-[14px]",
  "[&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_h4]:font-semibold",
  "[&_h2]:mt-[1.4em] [&_h2:first-child]:mt-0 [&_h3]:mt-[1.1em]",
  "[&_ul]:list-disc [&_ul]:pl-[1.4em] [&_ol]:list-decimal [&_ol]:pl-[1.4em] [&_li+li]:mt-[0.25em]",
  "[&_li.task-list-item]:-ml-[1.4em] [&_li.task-list-item]:list-none",
  "[&_code]:bg-paper-2 [&_code]:px-1 [&_code]:font-mono [&_code]:text-[12.5px]",
  "[&_pre]:overflow-x-auto [&_pre]:border [&_pre]:border-rule [&_pre]:bg-paper-2 [&_pre]:px-3.5 [&_pre]:py-3 [&_pre_code]:p-0",
  "[&_a]:text-act [&_a]:underline [&_blockquote]:border-l-2 [&_blockquote]:border-rule [&_blockquote]:pl-3 [&_blockquote]:text-ink-2",
].join(" ");

function SwitchView({ view, disabled, onPick }: { view: BodyView; disabled: boolean; onPick: (view: BodyView) => void }) {
  return (
    <div role="group" aria-label="Ticket view" className="mb-5 inline-flex border border-rule bg-paper [&>*+*]:border-l [&>*+*]:border-rule">
      {VIEWS.map((each) => (
        <button
          key={each.view}
          type="button"
          aria-pressed={each.view === view}
          disabled={disabled}
          onClick={() => onPick(each.view)}
          className={`inline-flex min-h-9 cursor-pointer items-center px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.08em] disabled:cursor-default disabled:opacity-60 ${
            each.view === view ? "bg-act-wash text-act" : "text-ink-3 hover:text-ink"
          }`}
        >
          {each.label}
        </button>
      ))}
    </div>
  );
}

const isTable = (value: Value): value is Table =>
  typeof value === "object" && !Array.isArray(value) && !(value instanceof HeaderDate);

function factText(value: Value): string {
  if (value instanceof HeaderDate) return value.iso;
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (isTable(value)) return typeof value.url === "string" ? value.url : "";
  if (value.length === 0) return "none";
  if (typeof value[0] === "string") return (value as string[]).join(", ");
  return (value as Table[]).map((entry) => entry.file).filter((file) => typeof file === "string").join(" · ");
}

/** `text` split into its header's keys and the Markdown after it, the title line dropped. */
function splitBody(text: string): { facts: [string, string][] | null; rest: string } {
  const span = headerSpan(text);
  const facts = span == null ? null : Object.entries(readHeader(text.slice(span.start, span.end))).map(([key, value]): [string, string] => [key, factText(value)]);
  const rest = (span == null ? text : text.slice(span.close)).replace(/^\s*# [^\n]*(?:\n|$)/, "");
  return { facts, rest };
}

function MarkdownView({ text }: { text: string }) {
  const { facts, rest } = splitBody(text);
  return (
    <>
      {facts != null && (
        <table aria-label="Ticket header" className="mb-6 border-collapse font-mono text-[12.5px] leading-[1.5]">
          <tbody>
            {facts.map(([key, value]) => (
              <tr key={key} className="border-b border-rule last:border-b-0">
                <th scope="row" className="py-1.5 pr-5 text-left align-baseline font-medium text-ink-3">{key}</th>
                <td className="py-1.5 align-baseline text-ink [overflow-wrap:anywhere]">{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className={RENDERED}>
        <Markdown>{rest}</Markdown>
      </div>
    </>
  );
}

function RawEditor({ ticketKey, base, revision, served, onClose }: { ticketKey: string; base: string; revision: number; served: string; onClose: () => void }) {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  const toast = useToast();
  const id = useId();
  const [opened] = useState(served);
  const [text, setText] = useState(served);
  const [busy, setBusy] = useState(false);
  const [moved, setMoved] = useState(false);
  const [refusal, setRefusal] = useState<string[]>([]);
  const save = async () => {
    setBusy(true);
    const result = await editTicket(base, ticketKey, revision, text, deps.fetch);
    setBusy(false);
    if (result.ok) {
      toast(`Saved ${ticketKey} as revision ${result.revision}.`);
      void client.invalidateQueries({ queryKey: ["fleet"] });
      void client.invalidateQueries({ queryKey: ["route", base] });
      onClose();
      return;
    }
    if ("conflict" in result) {
      setMoved(true);
      setRefusal([`${ticketKey} changed since this page read it: ${result.current != null ? `revision ${result.current}` : "a newer revision"} is current. Your text is kept.`]);
      void client.invalidateQueries({ queryKey: ["fleet"] });
      void client.invalidateQueries({ queryKey: ["route", base] });
      return;
    }
    setRefusal("problems" in result ? result.problems : [result.error]);
  };
  return (
    <div>
      <label htmlFor={id} className="block font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">
        Ticket body
      </label>
      <textarea
        id={id}
        value={text}
        onChange={(event) => setText(event.target.value)}
        spellCheck={false}
        rows={16}
        className="mt-2 block w-full resize-y [field-sizing:content] min-h-[16lh] border border-rule bg-paper-2 px-3.5 py-3 font-mono text-[12.5px] leading-[1.6] text-ink"
      />
      {refusal.length > 0 && (
        <div role="alert" className="mt-3">
          {refusal.map((line) => (
            <p key={line} className="m-0 max-w-[68ch] break-words font-mono text-[12.5px] leading-normal text-bad">{line}</p>
          ))}
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <Btn variant="primary" busy={busy} busyLabel="Saving…" disabled={text === opened || moved} onClick={() => void save()}>
          Save revision {revision + 1}
        </Btn>
        <Btn disabled={busy} onClick={onClose}>Discard</Btn>
      </div>
    </div>
  );
}

/** The left card's Fields / Markdown switch over `fields`, the whole of
 *  `text` rendered, or, while `editing` holds a revision, `text` raw in a
 *  box saved at that revision. */
export function TicketBody({ ticketKey, base, text, view, editing, onView, onClose, children }: {
  ticketKey: string; base: string; text: string; view: BodyView; editing: number | null;
  onView: (view: BodyView) => void; onClose: () => void; children: ReactNode;
}) {
  return (
    <div data-ticket-body>
      <SwitchView view={view} disabled={editing != null} onPick={onView} />
      {editing != null ? (
        <RawEditor ticketKey={ticketKey} base={base} revision={editing} served={text} onClose={onClose} />
      ) : view === "markdown" ? (
        <MarkdownView text={text} />
      ) : (
        children
      )}
    </div>
  );
}
