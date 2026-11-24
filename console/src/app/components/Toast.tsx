import { createContext, useCallback, useContext, useLayoutEffect, useState, type ReactNode } from "react";
import { createToaster, Toast, ToastAction, ToastRegion, ToastTitle } from "@ui-sdk/react/toast";

const TOAST_MS = 6000;
const MAX_VISIBLE = 3;
const ANNOUNCE_WAIT_MS = 100;
const ANNOUNCE_CAP_MS = 400;

type ToastActionItem = { label: string; run: () => void; valid?: () => boolean };
type Say = (text: string, action?: ToastActionItem) => void;

const ToastContext = createContext<Say>(() => {});

/** Adds a toast to the shell's stack. */
export function useToast(): Say {
  return useContext(ToastContext);
}

/** Re-queues announcements for toasts pushed past `MAX_VISIBLE` before the SDK's
 *  announcement debounce flushes; the SDK marks them announced on arrival and
 *  otherwise leaves them silent when promotion brings them back into view. */
function trackUnannounced(toaster: ReturnType<typeof createToaster<ReactNode>>) {
  const seen = new Set<string>();
  const pending = new Set<string>();
  const queued = new Set<string>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let waitingSince: number | undefined;

  const clearTimer = () => {
    clearTimeout(timer);
    timer = undefined;
    waitingSince = undefined;
  };

  const schedule = () => {
    const now = Date.now();
    waitingSince ??= now;
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = undefined;
      waitingSince = undefined;
      pending.clear();
    }, Math.max(0, Math.min(ANNOUNCE_WAIT_MS, waitingSince + ANNOUNCE_CAP_MS - now)));
  };

  const sync = () => {
    const all = toaster.toasts();
    const ids = new Set(all.map((item) => item.id));
    const visible = new Set(all.slice(0, MAX_VISIBLE).map((item) => item.id));
    for (const id of seen) {
      if (!ids.has(id)) {
        seen.delete(id);
        pending.delete(id);
        queued.delete(id);
      }
    }
    for (const id of pending) {
      if (!visible.has(id)) {
        pending.delete(id);
        queued.add(id);
      }
    }
    const promoted: string[] = [];
    for (const id of visible) {
      if (!seen.has(id)) {
        seen.add(id);
        pending.add(id);
      } else if (queued.delete(id)) {
        pending.add(id);
        promoted.push(id);
      }
    }
    if (promoted.length > 0) {
      for (const id of promoted) toaster.update(id, {});
      return;
    }
    if (pending.size > 0) schedule();
    else clearTimer();
  };

  const unsubscribe = toaster.subscribe(sync);
  sync();
  return () => {
    unsubscribe();
    clearTimer();
  };
}

/** The shell's toast region at the foot of the viewport: newest in front,
 *  each closing after `TOAST_MS` unless hovered or focused. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toaster] = useState(() => createToaster<ReactNode>({ duration: TOAST_MS }));
  const say = useCallback<Say>((text, action) => {
    toaster(text, action && {
      data: action.valid,
      action: {
        label: action.label,
        onAction: () => {
          if (!action.valid || action.valid()) action.run();
        },
      },
    });
  }, [toaster]);
  useLayoutEffect(() => {
    const stop = trackUnannounced(toaster);
    return () => {
      stop();
      toaster.dismiss();
    };
  }, [toaster]);
  return (
    <ToastContext.Provider value={say}>
      {children}
      <ToastRegion toaster={toaster} side="bottom" align="end" maxVisible={MAX_VISIBLE}>
        {(item) => {
          const valid = item.data as (() => boolean) | undefined;
          return (
            <Toast
              data-toast=""
              className="flex min-h-11 items-center justify-between gap-4 bg-ink px-4 py-2 font-mono text-[12.5px] font-medium leading-[1.4] text-paper"
            >
              <ToastTitle className="min-w-0" />
              {(!valid || valid()) && (
                <ToastAction className="shrink-0 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-current" />
              )}
            </Toast>
          );
        }}
      </ToastRegion>
    </ToastContext.Provider>
  );
}
