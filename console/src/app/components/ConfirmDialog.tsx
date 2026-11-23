import { useEffect, useRef, type ReactNode } from "react";
import { AlertDialog, AlertDialogBackdrop, AlertDialogClose, AlertDialogContent, AlertDialogTitle } from "@ui-sdk/react/alert-dialog";
import { Btn } from "./Btn";

const HEADING = "m-0 mt-2.5 font-sans text-[18px] font-semibold leading-[1.3] tracking-[-0.01em]";

/** A modal confirm labelled by its `heading` (or `ariaLabel` when set): focus
 *  starts on the cancel button (`cancelLabel`), returns there when the focused
 *  control leaves, and Tab stays inside; Esc or that button calls `onCancel`,
 *  and the `confirm` button (`primary` unless `confirmVariant`) calls
 *  `onConfirm` unless `confirmDisabled`, showing `busyLabel` while `busy`;
 *  with no `confirm` only the cancel button shows, and `cancelDisabled` holds
 *  the dialog open, Esc included. `refusal` reads under the buttons. */
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
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (document.activeElement == null || document.activeElement === document.body) cancel.current?.focus();
  }, [confirm != null, confirmDisabled, busy, cancelDisabled]);
  return (
    <AlertDialog
      open
      dismissible={!cancelDisabled}
      onOpenChange={(open) => {
        if (!open && !cancelDisabled) onCancel();
      }}
    >
      <AlertDialogBackdrop className="fixed inset-0 z-50 bg-[light-dark(color-mix(in_srgb,var(--scrim)_84%,transparent),rgba(3,0,8,0.62))]" />
      <AlertDialogContent
        aria-label={ariaLabel}
        className="fixed inset-0 z-50 m-auto box-border h-fit max-h-[calc(100dvh-2rem)] w-[min(480px,calc(100%-2rem))] overflow-y-auto overscroll-contain rounded-[12px] bg-raised px-[22px] pb-5 pt-[18px] text-left font-sans text-[14px] font-normal leading-normal text-ink outline-none shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))]"
      >
        <div className={`${labelClassName} text-[12px] leading-none text-ink-3`}>
          {label}
        </div>
        {ariaLabel ? <h2 className={HEADING}>{heading}</h2> : <AlertDialogTitle className={HEADING}>{heading}</AlertDialogTitle>}
        {children}
        <div className="mt-[22px] flex flex-wrap justify-end gap-2.5">
          <AlertDialogClose disabled={cancelDisabled} render={<Btn ref={cancel}>{cancelLabel}</Btn>} />
          {confirm != null && (
            <Btn variant={confirmVariant} disabled={confirmDisabled} busy={busy} busyLabel={busyLabel} onClick={onConfirm}>
              {confirm}
            </Btn>
          )}
        </div>
        {refusal}
      </AlertDialogContent>
    </AlertDialog>
  );
}
