import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ProgressLine } from "../progress";
import { FoldChevron } from "./icons";
import { ProgressLog } from "./ProgressLog";
import { trap } from "./trap";

function Panel({ run, lines, onClose }: { run: number; lines: ProgressLine[]; onClose: () => void }) {
  const titleId = useId();
  const box = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const live = useRef(onClose);
  live.current = onClose;
  useEffect(() => close.current?.focus(), []);
  useEffect(() => {
    const keep = (event: KeyboardEvent) => trap(event, box.current, () => live.current());
    document.addEventListener("keydown", keep, true);
    return () => document.removeEventListener("keydown", keep, true);
  }, []);
  return createPortal(
    <div className="fixed inset-0 z-50">
      <div data-scrim aria-hidden="true" onClick={onClose} className="absolute inset-0 bg-scrim" />
      <section
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="absolute inset-y-0 right-0 box-border flex w-[min(640px,100vw)] flex-col border-l-2 border-ink bg-paper text-ink shadow-[-8px_0_24px_rgba(0,0,0,.28)] max-[720px]:w-full max-[720px]:border-l-0"
      >
        <div className="flex min-h-14 items-center gap-2 border-b border-rule pl-5 pr-2.5">
          <h2 id={titleId} className="m-0 font-sans text-[16px] font-semibold leading-[1.3]">
            Raw log · run {run}
          </h2>
          <button
            ref={close}
            type="button"
            aria-label="Close raw log"
            onClick={onClose}
            className="ml-auto inline-flex size-10 cursor-pointer items-center justify-center border border-ink bg-paper text-ink hover:bg-paper-2"
          >
            <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" aria-hidden="true">
              <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
            </svg>
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-7 pt-1">
          <ProgressLog lines={lines} />
        </div>
      </section>
    </div>,
    document.body,
  );
}

/** The "Raw log N" button under the timeline, opening every Progress line,
 *  heartbeats included, in a modal panel docked right. */
export function RawLog({ run, lines }: { run: number; lines: ProgressLine[] }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const shut = () => {
    setOpen(false);
    button.current?.focus();
  };
  return (
    <>
      <button
        ref={button}
        type="button"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        className="group mt-4 inline-flex h-10 w-fit cursor-pointer items-center gap-2 border border-rule bg-paper px-4 font-mono text-[13px] text-ink hover:border-ink-3"
      >
        Raw log <span className="text-ink-3">{lines.length}</span>
        <FoldChevron open={false} />
      </button>
      {open && <Panel run={run} lines={lines} onClose={shut} />}
    </>
  );
}
