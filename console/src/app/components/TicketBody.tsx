import { useContext, useId, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Markdown } from "./Markdown";
import { editTicket } from "../../lib/boardWrites";
import { PollDepsContext } from "../scope";
import { HeaderDate, headerSpan, readHeader, type Table, type Value } from "../ticketHeader";
import { Btn } from "./Btn";
import { Segmented, type SegmentedOption } from "./Segmented";
import { useToast } from "./Toast";

export type BodyView = "fields" | "markdown";

const VIEWS: SegmentedOption<BodyView>[] = [
  { value: "fields", label: "Fields" },
  { value: "markdown", label: "Markdown" },
];

const RENDERED = [
  "max-w-[75ch] font-sans text-[14px] leading-[1.55] text-ink",
  "[&_.ticket-body>*+*]:mt-[0.7em] [&_p]:m-0",
  "[&_h1]:font-sans [&_h2]:font-sans [&_h3]:font-sans [&_h4]:font-sans",
  "[&_h1]:text-[17px] [&_h2]:text-[15px] [&_h3]:text-[14px] [&_h4]:text-[14px]",
  "[&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_h4]:font-semibold",
  "[&_h2]:mt-[1.4em] [&_h2:first-child]:mt-0 [&_h3]:mt-[1.1em]",
  "[&_ul]:list-disc [&_ul]:pl-[1.4em] [&_ol]:list-decimal [&_ol]:pl-[1.4em] [&_li+li]:mt-[0.25em]",
  "[&_li.task-list-item]:-ml-[1.4em] [&_li.task-list-item]:list-none",
  "[&_code]:rounded-[4px] [&_code]:bg-selected [&_code]:px-1 [&_code]:font-mono [&_code]:text-[12.5px]",
  "[&_pre]:overflow-x-auto [&_pre]:rounded-[6px] [&_pre]:bg-selected [&_pre]:px-3.5 [&_pre]:py-3 [&_pre_code]:rounded-none [&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_a]:text-act [&_a]:underline [&_blockquote]:border-l-2 [&_blockquote]:border-rule [&_blockquote]:pl-3 [&_blockquote]:text-ink-2",
].join(" ");

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
        <table aria-label="Ticket header" className="mb-6 w-full border-collapse font-sans text-[13px]">
          <tbody>
            {facts.map(([key, value]) => (
              <tr key={key} className="border-b border-hair">
                <th scope="row" className="h-[34px] w-[1%] whitespace-nowrap py-1.5 pr-6 text-left align-middle font-mono text-[12px] font-normal text-ink-3">{key}</th>
                <td className="h-[34px] py-1.5 align-middle text-ink-2 [overflow-wrap:anywhere]">{value}</td>
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
      <label htmlFor={id} className="block font-sans text-[13px] font-semibold leading-none text-ink">
        Ticket body
      </label>
      <textarea
        id={id}
        value={text}
        onChange={(event) => setText(event.target.value)}
        readOnly={busy}
        spellCheck={false}
        rows={16}
        className="mt-2 block w-full resize-y [field-sizing:content] min-h-[16lh] rounded-[6px] border-0 bg-selected px-3 py-2.5 font-mono text-[12.5px] leading-[1.6] text-ink outline-none focus:shadow-[0_0_0_2px_var(--wait)]"
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
      {editing == null && (
        <div className="mb-5">
          <Segmented label="Ticket view" options={VIEWS} value={view} onPick={onView} />
        </div>
      )}
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
