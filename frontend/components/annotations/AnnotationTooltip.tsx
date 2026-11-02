'use client';

import React from 'react';

interface AnnotationTooltipProps {
    label: string | null;
    x: number;
    y: number;
    visible: boolean;
    containerRef?: React.RefObject<HTMLElement>;
}

/**
 * Floating tooltip showing class label when hovering over annotations
 */
export function AnnotationTooltip({
    label,
    x,
    y,
    visible,
}: AnnotationTooltipProps) {
    if (!visible || !label) {
        return null;
    }

    // Offset tooltip slightly above and to the right of cursor
    const offsetX = 12;
    const offsetY = -24;

    return (
        <div
            className="absolute z-50 pointer-events-none px-2 py-1 rounded bg-black/80 text-white text-xs font-medium whitespace-nowrap shadow-lg border border-zinc-700"
            style={{
                left: x + offsetX,
                top: y + offsetY,
                transform: 'translateX(0)',
            }}
        >
            {label}
        </div>
    );
}

export default AnnotationTooltip;
