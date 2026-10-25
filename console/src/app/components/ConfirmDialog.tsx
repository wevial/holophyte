import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Btn } from "./Btn";
import { trap } from "./trap";

/** A modal confirm labelled by its `heading`: focus starts on the cancel
 *  button (`cancelLabel`) and Tab stays inside, Esc or that button calls
 *  `onCancel`, and the `confirm` button (`primary` unless `confirmVariant`)
 *  calls `onConfirm` unless `confirmDisabled`, showing `busyLabel` while
 *  `busy`; `refusal` reads under the buttons. */
export function ConfirmDialog({
  label,
  ariaLabel,
  labelClassName = "font-sans",
  heading,
  confirm,
  busyLabel,
  busy,
  onConfirm,
  onCancel,
  cancelLabel = "Cancel",
  confirmVariant = "primary",
  confirmDisabled = false,
  refusal = null,
  children,
}: {
  label: string;
  ariaLabel?: string;
  labelClassName?: string;
  heading: string;
  confirm: string;
  busyLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  cancelLabel?: string;
  confirmVariant?: "primary" | "danger-fill";
  confirmDisabled?: boolean;
  refusal?: ReactNode;
  children: ReactNode;
}) {
  const headingId = useId();
  const box = useRef<HTMLElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const cancelled = useRef(onCancel);
  cancelled.current = onCancel;
  useEffect(() => {
    cancel.current?.focus();
    const keep = (event: KeyboardEvent) => trap(event, box.current, cancelled.current);
    document.addEventListener("keydown", keep, true);
    return () => document.removeEventListener("keydown", keep, true);
  }, []);
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim p-4">
      <section
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        aria-labelledby={ariaLabel ? undefined : headingId}
        className="relative box-border w-full max-w-[520px] border border-rule bg-paper px-7 pb-6 pt-[26px] text-[15px] leading-normal text-ink shadow-[0_24px_60px_rgba(0,0,0,.45)]"
      >
        <div className={`${labelClassName} flex items-center gap-2.5 text-[11px] font-semibold uppercase leading-none tracking-[.06em] text-ink-3 after:flex-1 after:border-t after:border-rule-2 after:content-['']`}>
          {label}
        </div>
        <h2 id={headingId} className="mt-3.5 font-sans text-[30px] font-extrabold leading-[1.05] tracking-[-0.01em] [font-stretch:112%]">
          {heading}
        </h2>
        {children}
        <div className="mt-[22px] flex flex-wrap justify-end gap-2.5">
          <Btn ref={cancel} onClick={onCancel}>
            {cancelLabel}
          </Btn>
          <Btn variant={confirmVariant} disabled={confirmDisabled} busy={busy} busyLabel={busyLabel} onClick={onConfirm}>
            {confirm}
          </Btn>
        </div>
        {refusal}
      </section>
    </div>,
    document.body,
  );
}
