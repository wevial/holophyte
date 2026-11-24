import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger, type TooltipContentProps } from "@ui-sdk/react/tooltip";
import { TIP } from "./AgentPanel";

/** A hover and keyboard-focus hint over a trigger. Pass `describe` when the tip
 *  adds to the trigger's own name; omit it when it repeats or shortens it. */
export function Hint({ tip, side = "top", describe = false, children }: {
  tip: string;
  side?: TooltipContentProps["side"];
  describe?: boolean;
  children: (trigger: Record<string, unknown>) => ReactNode;
}) {
  return (
    <Tooltip delay={150} type="description">
      <TooltipTrigger
        render={({ type: _button, "aria-describedby": describedBy, ...trigger }) => (
          <>{children(describe ? { ...trigger, "aria-describedby": describedBy } : trigger)}</>
        )}
      />
      <TooltipContent side={side} sideOffset={6} className={TIP}>{tip}</TooltipContent>
    </Tooltip>
  );
}
