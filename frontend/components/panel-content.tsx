import React from 'react';
import { Cross2Icon, ViewVerticalIcon, ViewHorizontalIcon } from '@radix-ui/react-icons';

export type PanelType = 'grid' | 'filters' | 'empty' | 'embeddings' | 'map' | 'histogram' | 'duplicates' | 'concepts' | 'sparse-sample';

interface PanelContentProps {
    title: string;
    type: PanelType;
    onClose?: () => void;
    onSplit?: (direction: 'horizontal' | 'vertical') => void;
    onTypeChange?: (type: PanelType) => void;
    children?: React.ReactNode;
    isActive?: boolean;
}

export function PanelContent({ type, onClose, onSplit, onTypeChange, children, isActive }: PanelContentProps) {
    return (
        <div data-testid={`panel-${type}`} className={`flex flex-col h-full w-full bg-zinc-900 border border-zinc-800 rounded-md overflow-hidden ${isActive ? 'ring-1 ring-cyan-500' : ''}`}>
            <div className="flex items-center justify-between px-3 py-2 bg-zinc-800/50 border-b border-zinc-800 shrink-0">
                <div className="flex items-center gap-2">
                    <select
                        value={type}
                        onChange={(e) => onTypeChange?.(e.target.value as PanelType)}
                        data-testid="panel-type-select"
                        className="bg-zinc-800 border border-zinc-700 text-xs text-zinc-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                    >
                        <option value="empty">Select Panel...</option>
                        <option value="grid">Image Grid</option>
                        <option value="filters">Simple Filters</option>
                        <option value="embeddings">Embeddings</option>
                        <option value="map">Map</option>
                        <option value="histogram">Histogram</option>
                        <option value="duplicates">Duplicates</option>
                        <option value="concepts">Concepts</option>
                        <option value="sparse-sample">Sparse Sample</option>
                    </select>
                </div>

                <div className="flex items-center gap-1">
                    {onSplit && (
                        <>
                            <button
                                onClick={() => onSplit('horizontal')}
                                className="p-1 text-zinc-400 hover:text-white hover:bg-zinc-700 rounded"
                                title="Split Right (Side-by-Side)"
                            >
                                <ViewVerticalIcon className="w-3.5 h-3.5" />
                            </button>
                            <button
                                onClick={() => onSplit('vertical')}
                                className="p-1 text-zinc-400 hover:text-white hover:bg-zinc-700 rounded"
                                title="Split Down (Top-Bottom)"
                            >
                                <ViewHorizontalIcon className="w-3.5 h-3.5" />
                            </button>
                        </>
                    )}
                    {onClose && (
                        <button
                            onClick={onClose}
                            className="p-1 text-zinc-400 hover:text-red-400 hover:bg-zinc-700 rounded ml-1"
                            title="Close Panel"
                        >
                            <Cross2Icon className="w-3.5 h-3.5" />
                        </button>
                    )}
                </div>
            </div>
            <div className="flex-1 overflow-hidden relative">
                {children}
            </div>
        </div>
    );
}
