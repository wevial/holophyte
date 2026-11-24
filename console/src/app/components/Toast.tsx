import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState, type ReactNode } from "react";
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

/** Mirrors the SDK region's attention pauses so countdown accounting for
 *  re-announced toasts stays aligned with the region's own timers. */
function watchPause(el: HTMLElement | null, onChange: (paused: boolean) => void) {
  const reasons = new Set<string>();
  const set = (reason: string, active: boolean) => {
    const before = reasons.size > 0;
    if (active) reasons.add(reason);
    else reasons.delete(reason);
    const after = reasons.size > 0;
    if (before !== after) onChange(after);
  };
  const doc = el?.ownerDocument ?? document;
  const view = doc.defaultView ?? window;
  let blurTimer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;

  const onPointerEnter = (event: PointerEvent) => {
    if (event.pointerType !== "touch") set("item-hover", true);
  };
  const onPointerLeave = (event: PointerEvent) => {
    if (event.pointerType !== "touch") set("item-hover", false);
  };
  const onFocusIn = () => set("focus-in", true);
  const onFocusOut = (event: FocusEvent) => {
    const to = event.relatedTarget as Node | null;
    if (to) {
      if (el && !el.contains(to)) set("focus-in", false);
      return;
    }
    queueMicrotask(() => {
      if (!stopped && (!doc.hasFocus() || (el && !el.contains(doc.activeElement)))) set("focus-in", false);
    });
  };
  const onBlur = () => {
    clearTimeout(blurTimer);
    blurTimer = setTimeout(() => {
      blurTimer = undefined;
      if (doc.activeElement?.tagName !== "IFRAME") set("window-blur", true);
    });
  };
  const onFocus = () => {
    clearTimeout(blurTimer);
    blurTimer = undefined;
    set("window-blur", false);
  };
  const onVisibility = () => set("page-hide", doc.visibilityState === "hidden");

  el?.addEventListener("pointerenter", onPointerEnter);
  el?.addEventListener("pointerleave", onPointerLeave);
  el?.addEventListener("focusin", onFocusIn);
  el?.addEventListener("focusout", onFocusOut);
  view.addEventListener("blur", onBlur);
  view.addEventListener("focus", onFocus);
  doc.addEventListener("visibilitychange", onVisibility);
  if (doc.visibilityState === "hidden") set("page-hide", true);

  return () => {
    stopped = true;
    clearTimeout(blurTimer);
    el?.removeEventListener("pointerenter", onPointerEnter);
    el?.removeEventListener("pointerleave", onPointerLeave);
    el?.removeEventListener("focusin", onFocusIn);
    el?.removeEventListener("focusout", onFocusOut);
    view.removeEventListener("blur", onBlur);
    view.removeEventListener("focus", onFocus);
    doc.removeEventListener("visibilitychange", onVisibility);
  };
}

/** Re-queues announcements for toasts pushed past `MAX_VISIBLE` before the SDK's
 *  announcement debounce flushes, preserving each toast's remaining unpaused
 *  duration because `toaster.update` starts a fresh countdown on the version bump. */
function trackUnannounced(toaster: ReturnType<typeof createToaster<ReactNode>>, region: HTMLElement | null) {
  const seen = new Set<string>();
  const pending = new Set<string>();
  const queued = new Set<string>();
  const countdowns = new Map<string, { remaining: number; startedAt?: number }>();
  let paused = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let waitingSince: number | undefined;

  const stopEntry = (entry: { remaining: number; startedAt?: number }, now: number) => {
    if (entry.startedAt === undefined) return;
    entry.remaining = Math.max(0, entry.remaining - (now - entry.startedAt));
    entry.startedAt = undefined;
  };

  const applyCountdowns = (visible: Set<string>, now: number) => {
    for (const [id, entry] of countdowns) {
      if (!visible.has(id) || paused) stopEntry(entry, now);
      else if (entry.startedAt === undefined && Number.isFinite(entry.remaining)) entry.startedAt = now;
    }
  };

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
    const now = Date.now();
    const all = toaster.toasts();
    const ids = new Set(all.map((item) => item.id));
    const visible = new Set(all.slice(0, MAX_VISIBLE).map((item) => item.id));
    for (const id of countdowns.keys()) {
      if (!ids.has(id)) {
        seen.delete(id);
        pending.delete(id);
        queued.delete(id);
        countdowns.delete(id);
      }
    }
    for (const item of all) {
      if (!countdowns.has(item.id)) countdowns.set(item.id, { remaining: item.duration });
    }
    for (const id of pending) {
      if (!visible.has(id)) {
        pending.delete(id);
        queued.add(id);
      }
    }
    const promoted: { id: string; duration: number }[] = [];
    for (const id of visible) {
      if (!seen.has(id)) {
        seen.add(id);
        pending.add(id);
      } else if (queued.delete(id)) {
        pending.add(id);
        const entry = countdowns.get(id);
        if (entry) stopEntry(entry, now);
        promoted.push({ id, duration: entry?.remaining ?? TOAST_MS });
      }
    }
    applyCountdowns(visible, now);
    if (promoted.length > 0) {
      for (const item of promoted) toaster.update(item.id, { duration: item.duration });
      return;
    }
    if (pending.size > 0) schedule();
    else clearTimer();
  };

  const stopWatch = watchPause(region, (next) => {
    paused = next;
    const visible = new Set(toaster.toasts().slice(0, MAX_VISIBLE).map((item) => item.id));
    applyCountdowns(visible, Date.now());
  });
  const unsubscribe = toaster.subscribe(sync);
  sync();
  return () => {
    unsubscribe();
    stopWatch();
    clearTimer();
  };
}

/** The shell's toast region at the foot of the viewport: newest in front,
 *  each closing after `TOAST_MS` unless hovered or focused. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toaster] = useState(() => createToaster<ReactNode>({ duration: TOAST_MS }));
  const regionRef = useRef<HTMLElement>(null);
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
    const stop = trackUnannounced(toaster, regionRef.current);
    return () => {
      stop();
      toaster.dismiss();
    };
  }, [toaster]);
  return (
    <ToastContext.Provider value={say}>
      {children}
      <ToastRegion ref={regionRef} toaster={toaster} side="bottom" align="end" maxVisible={MAX_VISIBLE}>
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
