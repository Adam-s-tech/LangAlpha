import * as React from "react"
import * as PopoverPrimitive from "@radix-ui/react-popover"

import { cn } from "@/lib/utils"
import { lastInputWasPointer } from "@/lib/inputModality"

const Popover = PopoverPrimitive.Root

const PopoverTrigger = PopoverPrimitive.Trigger

const PopoverAnchor = PopoverPrimitive.Anchor

function PopoverContent({
  className,
  align = "center",
  sideOffset = 4,
  onCloseAutoFocus,
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        align={align}
        sideOffset={sideOffset}
        onCloseAutoFocus={(event) => {
          onCloseAutoFocus?.(event)
          // Radix hands focus back to the trigger on close so a keyboard user
          // keeps their place. Chromium carries :focus-visible across that
          // programmatic move, so a popover opened and dismissed with the mouse
          // leaves the trigger ringed until the next click. Skip the restore
          // when no key was pressed: focus falls to <body>, which is where
          // clicking anywhere else would have put it anyway.
          if (!event.defaultPrevented && lastInputWasPointer()) event.preventDefault()
        }}
        className={cn(
          "z-1030 w-72 rounded-md border bg-popover p-4 text-popover-foreground shadow-md outline-hidden pop-in",
          className
        )}
        {...props}
      />
    </PopoverPrimitive.Portal>
  )
}

export { Popover, PopoverTrigger, PopoverContent, PopoverAnchor }
