import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Screenshot } from "../../lib/contract.gen";
import { Btn } from "./Btn";
import { trap } from "./trap";

export type MediaItem = Pick<Screenshot, "caption" | "url"> & { kind: "image" | "video" };

function fileName(url: string) {
  const path = url.split(/[?#]/)[0]!;
  const name = path.slice(path.lastIndexOf("/") + 1);
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}

function StepButton({ label, disabled, onClick, className }: { label: "Previous" | "Next"; disabled: boolean; onClick: () => void; className: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={`${className} row-start-1 flex h-9 w-9 cursor-pointer items-center justify-center border border-rule bg-paper text-ink shadow-[0_2px_8px_rgba(0,0,0,.12)] hover:bg-paper-2 disabled:cursor-default disabled:text-ink-3 disabled:opacity-60 disabled:hover:bg-paper`}
    >
      <svg aria-hidden="true" viewBox="0 0 10 10" className="h-2.5 w-2.5 fill-none stroke-current stroke-[1.5]">
        <path d={label === "Previous" ? "M6.5 1.5 3 5l3.5 3.5" : "M3.5 1.5 7 5 3.5 8.5"} />
      </svg>
    </button>
  );
}

/** A modal over the console showing `items[start]`; Left and Right (or
 *  Previous and Next) step without wrapping, and Esc or Close calls
 *  `onClose` with the index shown last. */
export function MediaLightbox({ items, start, onClose }: { items: MediaItem[]; start: number; onClose: (index: number) => void }) {
  const [index, setIndex] = useState(start);
  const box = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const total = items.length;
  const shown = Math.min(index, total - 1);
  const live = useRef({ shown, onClose });
  live.current = { shown, onClose };
  useEffect(() => close.current?.focus(), []);
  useEffect(() => {
    const keep = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const step = event.key === "ArrowLeft" ? -1 : 1;
        setIndex((at) => Math.min(total - 1, Math.max(0, Math.min(at, total - 1) + step)));
        return;
      }
      trap(event, box.current, () => live.current.onClose(live.current.shown));
    };
    document.addEventListener("keydown", keep, true);
    return () => document.removeEventListener("keydown", keep, true);
  }, [total]);
  const item = items[shown]!;
  const name = `${item.kind === "video" ? "Video" : "Screenshot"} ${shown + 1} of ${total}`;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim p-2 sm:p-4">
      <section
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={name}
        className="grid w-full max-w-[1000px] grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 sm:gap-4"
      >
        <div className="col-start-2 row-start-1 border border-rule bg-paper text-ink shadow-[0_24px_60px_rgba(0,0,0,.45)]">
          <div className="flex items-center gap-3 border-b border-rule py-2 pl-3.5 pr-2">
            <span className="flex min-w-0 flex-1 gap-3 font-mono text-[12px] text-ink-2">
              <span className="shrink-0">{`${shown + 1} / ${total}`}</span>
              <span className="truncate">{fileName(item.url)}</span>
            </span>
            <Btn ref={close} aria-label="Close" onClick={() => onClose(shown)} className="!h-9 !px-3">
              <span aria-hidden="true">✕</span>
            </Btn>
          </div>
          <div className={`flex justify-center ${item.kind === "video" ? "bg-black" : "bg-paper-2"}`}>
            {item.kind === "video" ? (
              <video key={item.url} src={item.url} controls preload="metadata" className="block max-h-[70vh] w-full" />
            ) : (
              <img key={item.url} src={item.url} alt={item.caption} className="block max-h-[70vh] max-w-full object-contain" />
            )}
          </div>
          <p data-media-caption className="m-0 break-words px-3.5 py-3 font-read text-[14px] leading-[1.5] text-ink">
            {item.caption}
          </p>
        </div>
        <StepButton label="Previous" disabled={shown === 0} onClick={() => setIndex(shown - 1)} className="col-start-1" />
        <StepButton label="Next" disabled={shown === total - 1} onClick={() => setIndex(shown + 1)} className="col-start-3" />
      </section>
    </div>,
    document.body,
  );
}
