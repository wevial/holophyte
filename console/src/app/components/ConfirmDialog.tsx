import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AlertDialog, AlertDialogBackdrop, AlertDialogClose, AlertDialogContent, AlertDialogTitle } from "@ui-sdk/react/alert-dialog";
import { Btn } from "./Btn";

const HEADING = "m-0 mt-2.5 font-sans text-[18px] font-semibold leading-[1.3] tracking-[-0.01em]";

let mountedConfirms = 0;
let nativeFocus: typeof HTMLElement.prototype.focus | null = null;
let deferredFocus: { element: HTMLElement; options?: FocusOptions } | null = null;

// Callers that close by unmounting after an async confirm call opener.focus()
// before unmount lifts AlertDialog's inert lock; replay that target once inert clears.
function deferInertFocus(): () => void {
  if (mountedConfirms === 0) {
    const original = HTMLElement.prototype.focus;
    nativeFocus = original;
    HTMLElement.prototype.focus = function focus(options?: FocusOptions) {
      if (this.isConnected && this !== this.ownerDocument.body && this.closest("[inert]") != null) {
        deferredFocus = { element: this, options };
      } else if (deferredFocus?.element !== this) {
        deferredFocus = null;
      }
      return original.call(this, options);
    };
  }
  mountedConfirms += 1;
  return () => {
    mountedConfirms -= 1;
    if (mountedConfirms > 0) return;
    if (nativeFocus != null) {
      HTMLElement.prototype.focus = nativeFocus;
      nativeFocus = null;
    }
    const target = deferredFocus;
    deferredFocus = null;
    if (target == null) return;
    const active = document.activeElement;
    if ((active == null || active === document.body) && target.element.isConnected && target.element.closest("[inert]") == null) {
      target.element.focus(target.options);
    }
  };
}

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
  const [open, setOpen] = useState(true);
  const cancel = useRef<HTMLButtonElement>(null);
  const cancelled = useRef(onCancel);
  cancelled.current = onCancel;
  useEffect(deferInertFocus, []);
  // Child AlertDialog's layout effect releases inert and runs returnFocus before
  // this runs, so onCancel() focuses the caller's opener on a non-inert page.
  useLayoutEffect(() => {
    if (open) return;
    cancelled.current();
    setOpen(true);
  }, [open]);
  useEffect(() => {
    if (open && (document.activeElement == null || document.activeElement === document.body)) cancel.current?.focus();
  }, [open, confirm != null, confirmDisabled, busy, cancelDisabled]);
  return (
    <AlertDialog
      open={open}
      dismissible={!cancelDisabled}
      onOpenChange={(next, details) => {
        if (next) return;
        if (cancelDisabled) {
          details.cancel();
          return;
        }
        deferredFocus = null;
        setOpen(false);
      }}
    >
      <AlertDialogBackdrop className="fixed inset-0 z-50 bg-[light-dark(color-mix(in_srgb,var(--scrim)_84%,transparent),rgba(3,0,8,0.62))]" />
      <AlertDialogContent
        keepMounted
        aria-label={ariaLabel}
        onKeyDown={() => {
          deferredFocus = null;
        }}
        onPointerDown={() => {
          deferredFocus = null;
        }}
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
