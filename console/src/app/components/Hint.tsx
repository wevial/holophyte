import { useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { mergeProps } from "@ui-sdk/react/primitive";
import { Tooltip, TooltipContent, TooltipTrigger, type TooltipContentProps } from "@ui-sdk/react/tooltip";
import { TIP } from "./AgentPanel";

const WRAP_TIP = `${TIP} w-max max-w-[min(420px,var(--available-width,calc(100vw-16px)))] max-h-[var(--available-height,calc(100dvh-16px))] overflow-y-auto overscroll-contain cursor-auto whitespace-normal! leading-[1.35]! [overflow-wrap:anywhere]`;

const supportsTopLayer = () =>
  typeof HTMLElement !== "undefined" && typeof HTMLElement.prototype.showPopover === "function";

/** A hover and keyboard-focus hint over a trigger. Pass `describe` when the tip
 *  adds to the trigger's own name; omit it when it repeats or shortens it.
 *  Pass `wrap` for long tips that need to wrap and scroll within the viewport. */
export function Hint({ tip, side = "top", describe = false, wrap = false, children }: {
  tip: string;
  side?: TooltipContentProps["side"];
  describe?: boolean;
  wrap?: boolean;
  children: (trigger: Record<string, unknown>) => ReactNode;
}) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const restoreTabIndex = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(restoreTabIndex.current), [wrap]);

  const trackTrigger = useCallback((element: Element | null) => {
    if (!element) return;
    if (!supportsTopLayer()) {
      setContainer(null);
      return;
    }
    const interactive = element.parentElement?.closest("a, button");
    setContainer(interactive ? (interactive.parentElement ?? document.body) : null);
  }, []);

  const releasePointerFocus = wrap
    ? (event: MouseEvent<HTMLDivElement>) => {
        const popup = event.currentTarget;
        // Drop tabindex across mousedown's default focus step so dragging text cannot focus the popup, then restore -1 to stay out of Tab order.
        popup.removeAttribute("tabindex");
        clearTimeout(restoreTabIndex.current);
        restoreTabIndex.current = setTimeout(() => {
          if (popup.isConnected) popup.setAttribute("tabindex", "-1");
        }, 0);
      }
    : undefined;

  return (
    <Tooltip delay={150} type="description">
      <TooltipTrigger
        render={({ type: _button, "aria-describedby": describedBy, ...trigger }) => {
          const base = describe ? { ...trigger, "aria-describedby": describedBy } : trigger;
          return <>{children(mergeProps(base, { ref: trackTrigger }))}</>;
        }}
      />
      <TooltipContent
        side={side}
        sideOffset={6}
        container={container ?? undefined}
        tabIndex={wrap ? -1 : undefined}
        className={wrap ? WRAP_TIP : TIP}
        onMouseDown={releasePointerFocus}
        onFocus={wrap ? (event) => event.currentTarget.blur() : undefined}
        onClick={wrap ? (event) => { event.preventDefault(); event.stopPropagation(); } : undefined}
      >
        {tip}
      </TooltipContent>
    </Tooltip>
  );
}
