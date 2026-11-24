import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger, type TooltipContentProps } from "@ui-sdk/react/tooltip";
import { TIP } from "./AgentPanel";

const WRAP_TIP = `${TIP} w-max max-w-[min(420px,var(--available-width,calc(100vw-16px)))] max-h-[var(--available-height,calc(100dvh-16px))] overflow-y-auto overscroll-contain cursor-auto whitespace-normal! leading-[1.35]! [overflow-wrap:anywhere]`;

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
  return (
    <Tooltip delay={150} type="description">
      <TooltipTrigger
        render={({ type: _button, "aria-describedby": describedBy, ...trigger }) => (
          <>{children(describe ? { ...trigger, "aria-describedby": describedBy } : trigger)}</>
        )}
      />
      <TooltipContent
        side={side}
        sideOffset={6}
        className={wrap ? WRAP_TIP : TIP}
        onClick={wrap ? (event) => { event.preventDefault(); event.stopPropagation(); } : undefined}
      >
        {tip}
      </TooltipContent>
    </Tooltip>
  );
}
