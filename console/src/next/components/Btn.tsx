import type { ReactNode, Ref } from "react";

/** `primary` is one per row or dialog; `danger` opens a confirm whose own button is `danger-fill`. */
export type BtnVariant = "primary" | "quiet" | "danger" | "danger-fill";

export function Btn({
  variant = "quiet",
  busy = false,
  busyLabel,
  disabled = false,
  onClick,
  title,
  expanded,
  ref,
  children,
}: {
  variant?: BtnVariant;
  busy?: boolean;
  busyLabel?: string;
  disabled?: boolean;
  onClick?: () => void;
  title?: string;
  expanded?: boolean;
  ref?: Ref<HTMLButtonElement>;
  children: ReactNode;
}) {
  return (
    <button
      ref={ref}
      type="button"
      className="btn"
      data-variant={variant}
      aria-busy={busy}
      disabled={disabled}
      title={title}
      aria-expanded={expanded}
      onClick={busy ? undefined : onClick}
    >
      {busy && busyLabel ? busyLabel : children}
    </button>
  );
}
