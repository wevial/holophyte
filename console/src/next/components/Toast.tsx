import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

const TOAST_MS = 6000;

const ToastContext = createContext<(text: string) => void>(() => {});

/** Says `text` in the shell's one toast, replacing whatever it said. */
export function useToast(): (text: string) => void {
  return useContext(ToastContext);
}

/** The shell's toast: one sentence at a time at the foot of the viewport,
 *  gone after `TOAST_MS`. The live region stays mounted so a screen reader
 *  hears each new sentence. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [said, setSaid] = useState<{ text: string; at: number } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const say = useCallback((text: string) => {
    if (timer.current != null) clearTimeout(timer.current);
    setSaid({ text, at: Date.now() });
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
        className="pointer-events-none fixed bottom-6 right-6 z-40 max-w-[calc(100%-48px)]"
      >
        {said != null && (
          <p
            key={said.at}
            className="toast-in flex min-h-11 max-w-full items-center bg-ink px-4 py-2 font-mono text-[12.5px] font-medium leading-[1.4] text-paper"
          >
            {said.text}
          </p>
        )}
      </div>
    </ToastContext.Provider>
  );
}
