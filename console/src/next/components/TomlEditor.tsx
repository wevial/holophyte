import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { checkConfig } from "../../lib/config";
import { tomlTokenLines, type TomlKind } from "../tomlTokens";
import { PollDepsContext } from "../scope";
import { Btn } from "./Btn";

const KIND_CLASS: Record<TomlKind, string> = {
  table: "text-act", key: "text-ink", string: "text-live", number: "text-wait", bool: "text-wait", date: "text-wait", comment: "text-ink-3", punct: "text-ink-2", text: "text-ink",
};

function pageCaret(textarea: HTMLTextAreaElement, scroller: HTMLElement, step: 1 | -1, extend: boolean) {
  const { value, selectionStart, selectionEnd, selectionDirection } = textarea;
  const [anchor, focus] = selectionDirection === "backward" ? [selectionEnd, selectionStart] : [selectionStart, selectionEnd];
  const rows = Math.max(1, Math.floor(scroller.clientHeight / 20) - 1);
  const starts = [0, ...[...value.matchAll(/\n/g)].map(match => match.index + 1)];
  const line = starts.findLastIndex(start => start <= focus);
  const target = line + step * rows;
  const next = target < 0 ? 0 : target >= starts.length ? value.length
    : Math.min(starts[target]! + focus - starts[line]!, (starts[target + 1] ?? value.length + 1) - 1);
  if (extend) textarea.setSelectionRange(Math.min(anchor, next), Math.max(anchor, next), next < anchor ? "backward" : "forward");
  else textarea.setSelectionRange(next, next);
  const caretTop = 12 + Math.min(Math.max(target, 0), starts.length - 1) * 20;
  const paged = scroller.scrollTop + step * rows * 20;
  scroller.scrollTop = Math.min(Math.max(paged, caretTop + 32 - scroller.clientHeight), caretTop - 12);
}

export type TomlCheckState = { text: string; pending: boolean; error: string | null; line: number | null };

export function TomlEditor({ base, file, project, revision, original, text, onChange, onRevert, onSave, fieldChanges, disabled = false, busy = false, refusal, onCheck }: {
  base: string;
  file: string;
  project: string;
  revision: string;
  original: string;
  text: string;
  onChange: (text: string) => void;
  onRevert: () => void;
  onSave: () => void;
  fieldChanges: number;
  disabled?: boolean;
  busy?: boolean;
  refusal?: { text: string; error: string; line: number | null } | null;
  onCheck: (state: TomlCheckState) => void;
}) {
  const { fetch } = useContext(PollDepsContext);
  const [checked, setChecked] = useState<TomlCheckState | null>(null);
  const lines = useMemo(() => tomlTokenLines(text), [text]);
  const report = useRef(onCheck);
  report.current = onCheck;
  const changed = text !== original;
  const current: TomlCheckState = refusal?.text === text
    ? { ...refusal, pending: false }
    : !changed ? { text, pending: false, error: null, line: null }
    : checked?.text === text ? checked : { text, pending: true, error: null, line: null };

  useEffect(() => {
    let active = true;
    if (!changed) return;
    const timer = setTimeout(() => {
      void checkConfig(base, text, fetch).then((result) => {
        if (!active) return;
        setChecked({ text, pending: false, error: result.ok ? null : result.error, line: !result.ok && "line" in result ? result.line : null });
      });
    }, 500);
    return () => { active = false; clearTimeout(timer); };
  }, [base, text, changed, fetch]);

  useEffect(() => {
    report.current({ text, pending: current.pending, error: current.error, line: current.line });
  }, [text, current.pending, current.error, current.line]);

  const readOnly = disabled || busy || fieldChanges > 0;
  const gutterWidth = `max(3rem, calc(${String(lines.length).length}ch + 36px))`;
  const error = current.error?.replace(/^\[holo2\]\s+.*?:\s*/, "");
  return <div className="mt-6 min-w-0">
    {fieldChanges > 0 && <p className="mb-4 font-mono text-[12px] text-ink-3">Save or discard the {fieldChanges} field {fieldChanges === 1 ? "change" : "changes"} before editing the raw file.</p>}
    <div data-toml-scroller="" style={{ scrollPadding: `12px 0 12px calc(${gutterWidth} + 12px)` }} className="h-[360px] min-w-0 overflow-auto border border-rule bg-paper-2 font-mono text-[13px] focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-act">
      <div className="flex min-h-full w-max min-w-full">
        <div aria-hidden="true" style={{ width: gutterWidth }} className="sticky left-0 z-10 shrink-0 border-r border-rule bg-paper-2 py-3 text-right font-mono text-[13px] leading-5 text-ink-3">
          {lines.map((_, index) => <div key={index} data-line={index + 1} data-refused={current.line === index + 1 ? "true" : undefined} className={`h-5 px-3 ${current.line === index + 1 ? "bg-bad-wash text-bad" : ""}`}>
            <span className="mr-1 inline-block w-1.5">{current.line === index + 1 ? "▪" : ""}</span>{index + 1}
          </div>)}
        </div>
        <div className="relative flex-1">
          {current.line != null && <div data-refused-band="" className="absolute inset-x-0 h-5 bg-bad-wash" style={{ top: 12 + (current.line - 1) * 20 }} />}
          <pre aria-hidden="true" className="relative m-0 py-3 pl-3 pr-8 font-mono text-[13px] leading-5 whitespace-pre">
            {lines.map((spans, index) => <span key={index} data-text-line={index + 1} className="block h-5">
              {spans.map((span, at) => <span key={at} data-kind={span.kind} className={KIND_CLASS[span.kind]}>{span.text}</span>)}
            </span>)}
          </pre>
          <textarea
            aria-label={`${file} for ${project}`}
            aria-invalid={current.error != null}
            value={text}
            readOnly={readOnly}
            rows={lines.length}
            wrap="off"
            spellCheck={false}
            onChange={(event) => { setChecked(null); onChange(event.target.value); }}
            onKeyDown={(event) => {
              if ((event.key !== "PageDown" && event.key !== "PageUp") || event.altKey || event.ctrlKey || event.metaKey) return;
              event.preventDefault();
              pageCaret(event.currentTarget, event.currentTarget.closest<HTMLElement>("[data-toml-scroller]")!, event.key === "PageDown" ? 1 : -1, event.shiftKey);
            }}
            className="absolute inset-0 block h-full w-full resize-none overflow-hidden border-0 bg-transparent px-3 py-3 font-mono text-[13px] leading-5 whitespace-pre text-transparent caret-ink outline-none selection:bg-act/25"
          />
        </div>
      </div>
    </div>
    <div aria-live="polite" className="mt-5">
      <h3 className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">Checker</h3>
      <p className={`mt-2 font-mono text-[12px] ${current.pending ? "text-ink-3" : current.error ? "text-bad" : "text-live"}`}>{current.pending ? "checking…" : current.error ? "1 error blocks saving" : "no errors · ready to save"}</p>
      {error && <div className="mt-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 border border-bad bg-bad-wash px-3 py-3 font-mono text-[12px]">
        <span aria-hidden="true" className="text-bad">×</span>
        {current.line != null && <span className="text-ink-3">line {current.line}</span>}
        <span className="text-bad">error</span><span className="min-w-0 break-words text-ink">{error}</span>
      </div>}
    </div>
    <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-rule pt-4">
      <Btn disabled={!changed || busy} onClick={onRevert}>Revert to revision {revision.slice(0, 7)}</Btn>
      <Btn variant="primary" disabled={readOnly || !changed || current.pending || current.error != null} busy={busy} busyLabel="Saving…" onClick={onSave}>Save</Btn>
      <p className="font-mono text-[12px] text-ink-3">{current.error ? "Fix the error to save." : "Saving shows the diff first."}</p>
    </div>
  </div>;
}
