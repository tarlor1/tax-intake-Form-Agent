"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import { Progress as ProgressPrimitive } from "radix-ui"

function Progress({
  className,
  value,
  max = 100,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root>) {
  const indeterminate = value === undefined || value === null;
  const percent = indeterminate ? 0 : Math.min(100, Math.max(0, value / max * 100));

  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      value={value ?? null}
      max={max}
      className={cn(
        "relative h-2 w-full overflow-hidden rounded-full bg-primary/20",
        className
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn(
          "h-full bg-primary motion-reduce:transition-none",
          indeterminate
            ? "w-2/5 animate-intake-progress motion-reduce:animate-none"
            : "w-full transition-transform"
        )}
        style={indeterminate ? undefined : { transform: `translateX(-${100 - percent}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

export { Progress }
