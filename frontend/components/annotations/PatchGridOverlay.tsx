'use client';

import { useState, memo } from 'react';

interface PatchGridOverlayProps {
    scale: string;  // "2x2", "4x4", etc.
    selectedPatches?: Array<{ row: number; col: number }>;
    highlightedPatch?: { row: number; col: number } | null;
    isGridSelected?: boolean;
    onPatchClick?: (row: number, col: number) => void;
    mode: 'select' | 'highlight';
}

function parseScale(scale: string): { rows: number; cols: number } {
    const parts = scale.split('x');
    return { rows: parseInt(parts[0], 10), cols: parseInt(parts[1], 10) };
}

export const PatchGridOverlay = memo(function PatchGridOverlay({
    scale,
    selectedPatches: selectedPatchesArr,
    highlightedPatch,
    isGridSelected,
    onPatchClick,
    mode,
}: PatchGridOverlayProps) {
    const [hoveredPatch, setHoveredPatch] = useState<{ row: number; col: number } | null>(null);
    const { rows, cols } = parseScale(scale);

    if (mode === 'highlight' && highlightedPatch) {
        // Pure CSS positioning — no image dimensions needed, works in any container
        const pctW = 100 / cols;
        const pctH = 100 / rows;
        const fillColor = isGridSelected ? 'rgba(245, 158, 11, 0.45)' : 'rgba(0, 200, 255, 0.35)';
        const borderColor = isGridSelected ? '#f59e0b' : '#00c8ff';
        return (
            <div
                className="absolute"
                style={{
                    left: `${highlightedPatch.col * pctW}%`,
                    top: `${highlightedPatch.row * pctH}%`,
                    width: `${pctW}%`,
                    height: `${pctH}%`,
                    backgroundColor: fillColor,
                    border: `2px solid ${borderColor}`,
                    cursor: 'pointer',
                    zIndex: 5,
                }}
                onClick={(e) => {
                    e.stopPropagation();
                    onPatchClick?.(highlightedPatch.row, highlightedPatch.col);
                }}
            />
        );
    }

    // Select mode: full interactive CSS grid
    const pctW = 100 / cols;
    const pctH = 100 / rows;

    return (
        <div className="absolute inset-0" style={{ zIndex: 5 }}>
            {Array.from({ length: rows }, (_, r) =>
                Array.from({ length: cols }, (_, c) => {
                    const isSelected = selectedPatchesArr?.some(p => p.row === r && p.col === c) ?? false;
                    const isHovered = hoveredPatch?.row === r && hoveredPatch?.col === c;

                    return (
                        <div
                            key={`${r}-${c}`}
                            className="absolute cursor-pointer transition-colors"
                            style={{
                                left: `${c * pctW}%`,
                                top: `${r * pctH}%`,
                                width: `${pctW}%`,
                                height: `${pctH}%`,
                                backgroundColor: isSelected
                                    ? 'rgba(0, 200, 255, 0.3)'
                                    : isHovered
                                        ? 'rgba(255, 255, 255, 0.15)'
                                        : 'transparent',
                                border: isSelected
                                    ? '2px solid #00c8ff'
                                    : '1px solid rgba(255,255,255,0.25)',
                            }}
                            onMouseEnter={() => setHoveredPatch({ row: r, col: c })}
                            onMouseLeave={() => setHoveredPatch(null)}
                            onClick={(e) => {
                                e.stopPropagation();
                                onPatchClick?.(r, c);
                            }}
                        />
                    );
                })
            )}
        </div>
    );
});
