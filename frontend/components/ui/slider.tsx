import * as React from "react"
import * as SliderPrimitive from "@radix-ui/react-slider"

const Slider = React.forwardRef<
    React.ElementRef<typeof SliderPrimitive.Root>,
    React.ComponentPropsWithoutRef<typeof SliderPrimitive.Root>
>(({ className, ...props }, ref) => (
    <SliderPrimitive.Root
        ref={ref}
        className={`relative flex w-full touch-none select-none items-center ${className}`}
        {...props}
    >
        <SliderPrimitive.Track className="relative h-1.5 w-full grow overflow-hidden rounded-full bg-zinc-800">
            <SliderPrimitive.Range className="absolute h-full bg-cyan-500" />
        </SliderPrimitive.Track>
        <SliderPrimitive.Thumb className="block h-4 w-4 rounded-full border border-cyan-500/50 bg-cyan-950 ring-offset-zinc-950 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 hover:bg-cyan-900" />
    </SliderPrimitive.Root>
))
Slider.displayName = SliderPrimitive.Root.displayName

export { Slider }
