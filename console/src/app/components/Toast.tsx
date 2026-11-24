import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createToaster, Toast, ToastAction, ToastRegion, ToastTitle } from "@ui-sdk/react/toast";

const TOAST_MS = 6000;

type Say = (text: string, action?: { label: string; run: () => void }) => void;

const ToastContext = createContext<Say>(() => {});

/** Adds a toast to the shell's stack. */
export function useToast(): Say {
  return useContext(ToastContext);
}

/** The shell's toast region at the foot of the viewport: newest in front,
 *  each closing after `TOAST_MS` unless hovered or focused. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toaster] = useState(() => createToaster({ duration: TOAST_MS }));
  const latest = useRef(0);
  const say = useCallback<Say>((text, action) => {
    const seq = ++latest.current;
    toaster(text, action && {
      data: seq,
      action: {
        label: action.label,
        onAction: () => {
          if (latest.current === seq) action.run();
        },
      },
    });
  }, [toaster]);
  useEffect(() => () => toaster.dismiss(), [toaster]);
  return (
    <ToastContext.Provider value={say}>
      {children}
      <ToastRegion toaster={toaster} side="bottom" align="end">
        {(item) => (
          <Toast
            data-toast=""
            className="flex min-h-11 items-center justify-between gap-4 bg-ink px-4 py-2 font-mono text-[12.5px] font-medium leading-[1.4] text-paper"
          >
            <ToastTitle className="min-w-0" />
            {item.data === latest.current && (
              <ToastAction className="shrink-0 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-current" />
            )}
          </Toast>
        )}
      </ToastRegion>
    </ToastContext.Provider>
  );
}
