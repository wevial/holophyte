import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Btn } from "./Btn";
import { trap } from "./trap";

/** A modal confirm labelled by its `heading`: focus starts on the cancel
 *  button (`cancelLabel`), returns there when the focused control leaves,
 *  and Tab stays inside; Esc or that button calls `onCancel`, and the
 *  `confirm` button (`primary` unless `confirmVariant`) calls `onConfirm`
 *  unless `confirmDisabled`, showing `busyLabel` while `busy`; with no
 *  `confirm` only the cancel button shows, and `cancelDisabled` holds the
 *  dialog open, Esc included. `refusal` reads under the buttons. */
export function ConfirmDialog({
  label,
  ariaLabel,
  labelClassName = "font-mono",
  heading,
  confirm,
  busyLabel,
  busy,
  onConfirm,
  onCancel,
  cancelLabel = "Cancel",
  confirmVariant = "primary",
  confirmDisabled = false,
  cancelDisabled = false,
  refusal = null,
  children,
}: {
  label: string;
  ariaLabel?: string;
  labelClassName?: string;
  heading: string;
  confirm?: string;
  busyLabel?: string;
  busy?: boolean;
  onConfirm?: () => void;
  onCancel: () => void;
  cancelLabel?: string;
  confirmVariant?: "primary" | "danger-fill";
  confirmDisabled?: boolean;
  cancelDisabled?: boolean;
  refusal?: ReactNode;
  children: ReactNode;
}) {
  const headingId = useId();
  const box = useRef<HTMLElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const cancelled = useRef(onCancel);
  cancelled.current = cancelDisabled ? () => {} : onCancel;
  useEffect(() => {
    cancel.current?.focus();
    const keep = (event: KeyboardEvent) => trap(event, box.current, cancelled.current);
    document.addEventListener("keydown", keep, true);
    return () => document.removeEventListener("keydown", keep, true);
  }, []);
  useEffect(() => {
    if (document.activeElement == null || document.activeElement === document.body) cancel.current?.focus();
  }, [confirm != null, confirmDisabled, busy, cancelDisabled]);
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[light-dark(color-mix(in_srgb,var(--scrim)_84%,transparent),rgba(3,0,8,0.62))] p-4">
      <section
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        aria-labelledby={ariaLabel ? undefined : headingId}
        className="relative box-border max-h-full w-full max-w-[480px] overflow-y-auto overscroll-contain rounded-[12px] bg-raised px-[22px] pb-5 pt-[18px] text-[14px] leading-normal text-ink shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))]"
      >
        <div className={`${labelClassName} text-[12px] leading-none text-ink-3`}>
          {label}
        </div>
        <h2 id={headingId} className="m-0 mt-2.5 font-sans text-[18px] font-semibold leading-[1.3] tracking-[-0.01em]">
          {heading}
        </h2>
        {children}
        <div className="mt-[22px] flex flex-wrap justify-end gap-2.5">
          <Btn ref={cancel} disabled={cancelDisabled} onClick={onCancel}>
            {cancelLabel}
          </Btn>
          {confirm != null && (
            <Btn variant={confirmVariant} disabled={confirmDisabled} busy={busy} busyLabel={busyLabel} onClick={onConfirm}>
              {confirm}
            </Btn>
          )}
        </div>
        {refusal}
      </section>
    </div>,
    document.body,
  );
}
