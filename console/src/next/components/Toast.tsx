import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

const TOAST_MS = 6000;

type ToastAction = { label: string; run: () => void };
type Say = (text: string, action?: ToastAction) => void;

const ToastContext = createContext<Say>(() => {});

/** Says `text` in the shell's one toast, replacing whatever it said. */
export function useToast(): Say {
  return useContext(ToastContext);
}

/** The shell's toast: one sentence at a time at the foot of the viewport,
 *  gone after `TOAST_MS`. The live region stays mounted so a screen reader
 *  hears each new sentence. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [said, setSaid] = useState<{ text: string; at: number; action?: ToastAction } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const say = useCallback((text: string, action?: ToastAction) => {
    if (timer.current != null) clearTimeout(timer.current);
    setSaid({ text, at: Date.now(), action });
    timer.current = setTimeout(() => setSaid(null), TOAST_MS);
  }, []);
  useEffect(() => () => {
    if (timer.current != null) clearTimeout(timer.current);
  }, []);
  return (
    <ToastContext.Provider value={say}>
      {children}
      <div
        role="status"
        aria-live="polite"
        data-toast
        className={`${said?.action ? "pointer-events-auto" : "pointer-events-none"} fixed bottom-6 right-6 z-40 max-w-[calc(100%-48px)]`}
      >
        {said != null && (
          <p
            key={said.at}
            className="toast-in flex min-h-11 max-w-full items-center bg-ink px-4 py-2 font-mono text-[12.5px] font-medium leading-[1.4] text-paper"
          >
            {said.text}
            {said.action && <button
              type="button"
              className="ml-4 shrink-0 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-current"
              onClick={() => {
                if (timer.current != null) clearTimeout(timer.current);
                setSaid(null);
                said.action?.run();
              }}
            >{said.action.label}</button>}
          </p>
        )}
      </div>
    </ToastContext.Provider>
  );
}
