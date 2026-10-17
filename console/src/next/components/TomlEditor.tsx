import { useContext, useEffect, useRef, useState } from "react";
import { checkConfig } from "../../lib/config";
import { PollDepsContext } from "../scope";
import { Btn } from "./Btn";

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
  const [scrollTop, setScrollTop] = useState(0);
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
  const error = current.error?.replace(/^\[holo2\]\s+.*?:\s*/, "");
  const markTop = current.line == null ? null : 12 + (current.line - 1) * 20 - scrollTop;
  return <div className="mt-6 min-w-0">
    {fieldChanges > 0 && <p className="mb-4 font-mono text-[12px] text-ink-3">Save or discard the {fieldChanges} field {fieldChanges === 1 ? "change" : "changes"} before editing the raw file.</p>}
    <div className="flex min-w-0 overflow-hidden border border-rule bg-paper-2">
      <div aria-hidden="true" className="max-h-[360px] shrink-0 overflow-hidden border-r border-rule py-3 text-right font-mono text-[13px] leading-5 text-ink-3">
        <div style={{ transform: `translateY(-${scrollTop}px)` }}>
        {text.split("\n").map((_, index) => <div key={index} data-line={index + 1} data-refused={current.line === index + 1 ? "true" : undefined} className={`h-5 min-w-12 px-3 ${current.line === index + 1 ? "bg-bad-wash text-bad" : ""}`}>
          <span className="mr-1 inline-block w-1.5">{current.line === index + 1 ? "▪" : ""}</span>{index + 1}
        </div>)}
        </div>
      </div>
      <textarea
        aria-label={`${file} for ${project}`}
        aria-invalid={current.error != null}
        value={text}
        readOnly={readOnly}
        wrap="off"
        spellCheck={false}
        onChange={(event) => { setChecked(null); onChange(event.target.value); }}
        onScroll={(event) => { setScrollTop(event.currentTarget.scrollTop); }}
        className="block h-[360px] min-w-0 flex-1 resize-none overflow-auto border-0 bg-transparent px-3 py-3 font-mono text-[13px] leading-5 text-ink outline-offset-[-2px] focus:outline-act"
        style={markTop == null ? undefined : { backgroundImage: `linear-gradient(to bottom, transparent ${markTop}px, var(--bad-wash) ${markTop}px, var(--bad-wash) ${markTop + 20}px, transparent ${markTop + 20}px)` }}
      />
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
