import type { ReactNode } from "react";

/** `primary` is one per row or dialog; `danger` opens a confirm whose own button is `danger-fill`. */
export type BtnVariant = "primary" | "quiet" | "danger" | "danger-fill";

export function Btn({
  variant = "quiet",
  busy = false,
  busyLabel,
  disabled = false,
  onClick,
  children,
}: {
  variant?: BtnVariant;
  busy?: boolean;
  busyLabel?: string;
  disabled?: boolean;
  onClick?: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="btn"
      data-variant={variant}
      aria-busy={busy}
      disabled={disabled}
      onClick={busy ? undefined : onClick}
    >
      {busy && busyLabel ? busyLabel : children}
    </button>
  );
}
