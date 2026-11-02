'use client';

import { memo, useId, useMemo, useState } from 'react';
import type { Calibration, Cuboid } from '@/lib/labels';
import type { FieldColorConfig } from '@/lib/api';
import { generateColor } from '@/lib/annotation-utils';
import { projectCuboid } from '@/lib/geometry3d';

interface Props {
    cuboids: Cuboid[];
    calibration: Calibration;
    imageSize: { width: number; height: number };
    colorConfig?: FieldColorConfig;
    hiddenIndices?: Set<number>;
    selectedIndex?: number;
    onSelect?: (index: number) => void;
    onHover?: (label: string | null, x: number, y: number, event?: React.MouseEvent) => void;
}

export const CuboidRenderer = memo(function CuboidRenderer({ cuboids, calibration, imageSize, colorConfig, hiddenIndices, selectedIndex, onSelect, onHover }: Props) {
    const [hovered, setHovered] = useState<number | null>(null);
    const clipId = useId();
    const projections = useMemo(() => cuboids.map(box => box ? projectCuboid(box, calibration, imageSize.width) : null),
        [cuboids, calibration, imageSize.width]);
    return <>
        <defs><clipPath id={clipId}><rect width={imageSize.width} height={imageSize.height} /></clipPath></defs>
        <g clipPath={`url(#${clipId})`}>{cuboids.map((box, index) => {
            const projected = projections[index];
            if (!box || hiddenIndices?.has(index) || !projected?.extent) return null;
            const selected = selectedIndex === index;
            const active = selected || hovered === index;
            const label = typeof box.label === 'string' ? box.label : '';
            const color = colorConfig?.valueColors?.[label] || generateColor(label);
            const tooltip = `${label} · ${projected.distance.toFixed(1)} m${box.occluded == null ? '' : ` · occluded ${box.occluded}`}`;
            const hover = (event: React.MouseEvent<SVGGElement>) => {
                const rect = event.currentTarget.ownerSVGElement?.getBoundingClientRect();
                if (rect) onHover?.(tooltip, event.clientX - rect.left, event.clientY - rect.top, event);
            };
            return <g key={index} data-cuboid-id={box.id} data-selected={selected}
                className="cursor-pointer" pointerEvents="none" aria-label={tooltip}
                onMouseEnter={event => { setHovered(index); hover(event); }}
                onMouseMove={hover}
                onMouseLeave={() => { setHovered(null); onHover?.(null, 0, 0); }}
                onClick={event => { if (onSelect) { event.stopPropagation(); onSelect(index); } }}>
                {projected.edges.map(([a, b], i) => <line key={`hit-${i}`}
                    x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke="transparent"
                    strokeWidth={10} pointerEvents="stroke" vectorEffect="non-scaling-stroke" />)}
                {projected.edges.map(([a, b], i) => <line key={`edge-${i}`} data-cuboid-edge={i}
                    x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={selected ? 'cyan' : color}
                    strokeWidth={active ? 3 : 2} strokeOpacity={colorConfig?.opacity ?? 0.9} vectorEffect="non-scaling-stroke" />)}
                {projected.heading.map(([a, b], i) => <line key={`heading-${i}`} data-cuboid-heading={i}
                    x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={color}
                    strokeWidth={1} vectorEffect="non-scaling-stroke" />)}
            </g>;
        })}</g></>;
});
