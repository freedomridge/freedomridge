'use client';

import type { Classification } from '@/lib/labels';
import { formatConfidence } from '@/lib/annotation-utils';

interface Props {
    classifications: Classification[];
    maxVisible?: number;
    /** Field name shown as the badge header — disambiguates multiple classification fields. */
    fieldName?: string;
    /** Compact grid variant: a small label chip instead of the full modal badge box. */
    compact?: boolean;
}

export function ClassificationRenderer({ classifications, maxVisible = 5, fieldName, compact = false }: Props) {
    // Sort by confidence (highest first)
    const sorted = [...classifications].sort((a, b) =>
        (b.confidence || 0) - (a.confidence || 0)
    );

    if (compact) {
        // Grid variant: a single small chip (labels only). The parent container
        // positions it; no min-width / absolute so it never overflows a small tile
        // or collides with the cell checkbox.
        const labels = sorted.slice(0, 3).map(c => c.label).join(', ');
        return (
            <div
                className="bg-black/70 rounded px-1.5 py-0.5 text-[9px] leading-tight text-white/90 max-w-full truncate"
                title={fieldName ? `${fieldName}: ${labels}` : labels}
            >
                {labels}{sorted.length > 3 ? '…' : ''}
            </div>
        );
    }

    const visible = sorted.slice(0, maxVisible);

    return (
        <div className="bg-black/80 rounded-lg p-3 backdrop-blur-sm border border-white/10 min-w-[220px]">
            <h4 className="text-xs font-semibold text-zinc-400 mb-2 uppercase tracking-wide truncate">
                {fieldName || 'Classifications'}
            </h4>
            <div className="space-y-2">
                {visible.map((cls, idx) => (
                    <div key={idx} className="space-y-1">
                        <div className="flex items-center justify-between">
                            <span className="text-sm text-white font-medium">
                                {cls.label}
                            </span>
                            {cls.confidence !== undefined && (
                                <span className="text-xs text-zinc-400 ml-2">
                                    {formatConfidence(cls.confidence)}
                                </span>
                            )}
                        </div>

                        {/* Confidence bar */}
                        {cls.confidence !== undefined && (
                            <div className="h-1.5 bg-zinc-700/50 rounded-full overflow-hidden">
                                <div
                                    className="h-full bg-gradient-to-r from-cyan-500 to-cyan-400 transition-all"
                                    style={{ width: `${cls.confidence * 100}%` }}
                                />
                            </div>
                        )}

                        {/* Attributes */}
                        {cls.attributes && Object.keys(cls.attributes).length > 0 && (
                            <div className="mt-1 pl-2 border-l-2 border-zinc-700">
                                {Object.entries(cls.attributes).slice(0, 3).map(([key, value]) => (
                                    <div key={key} className="flex justify-between text-xs">
                                        <span className="text-zinc-500">{key}:</span>
                                        <span className="text-zinc-300">{String(value)}</span>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {sorted.length > maxVisible && (
                <div className="mt-2 text-xs text-zinc-500 text-center">
                    +{sorted.length - maxVisible} more
                </div>
            )}
        </div>
    );
}
