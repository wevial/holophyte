import { useEffect, useRef, useState, type ReactNode } from "react";

/** The reason every action is inert until the write ticket lands. */
export const WRITES_LATER = "Writes arrive later behind a token";

/** A row action. Without `onAct` it is inert (`disabled`, titled
 *  `WRITES_LATER` or the given `title`). With one, a click runs it and
 *  the button spins, disabled, until the promise settles; a `disabled`
 *  prop keeps it inert with `title` saying why. */
export function ActionButton({
  children,
  onAct,
  disabled,
  title,
}: {
  children: ReactNode;
  onAct?: () => Promise<unknown>;
  disabled?: boolean;
  title?: string;
}) {
  const [pending, setPending] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const inert = !onAct || disabled === true;
  const click = onAct
    ? async () => {
        setPending(true);
        try {
          await onAct();
        } finally {
          if (mounted.current) setPending(false);
        }
      }
    : undefined;
  return (
    <button
      type="button"
      disabled={inert || pending}
      aria-busy={pending || undefined}
      title={inert ? (title ?? WRITES_LATER) : undefined}
      onClick={click}
      className={`rounded-button border border-chip-border px-2 py-1 text-[12px] font-semibold ${
        inert ? "text-muted opacity-60" : "text-ink hover:bg-chip-border/40"
      }`}
    >
      {pending && (
        <span data-spinner aria-hidden className="mr-1 inline-block animate-spin">
          ◌
        </span>
      )}
      {children}
    </button>
  );
}
