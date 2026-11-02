import React, { useState } from 'react';
import { ViewStage, SchemaField } from '@/lib/api';
import { Cross2Icon } from '@radix-ui/react-icons';
import { FilterStageEditor } from './filter-stage-editor';

interface FilterBarProps {
    stages: ViewStage[];
    onRemoveStage: (index: number) => void;
    onUpdateStage: (index: number, stage: ViewStage) => void;
    onClearAll: () => void;
    datasetId: number;
    schema: SchemaField[];
}

export function FilterBar({ stages, onRemoveStage, onUpdateStage, onClearAll, datasetId, schema }: FilterBarProps) {
    const [editingIndex, setEditingIndex] = useState<number | null>(null);

    if (!stages || stages.length === 0) return null;

    return (
        <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-zinc-500 font-medium">Applied Filters:</span>

            {stages.map((stage, index) => (
                <div key={index} className="relative">
                    <div
                        className={`
                            flex items-center gap-1.5 px-2 py-1 rounded text-xs border animate-in fade-in slide-in-from-left-2 cursor-pointer hover:opacity-80 transition-opacity
                            ${stage.type === 'selection' ? 'bg-cyan-900/30 text-cyan-300 border-cyan-800' : ''}
                            ${stage.type === 'map_region' ? 'bg-purple-900/30 text-purple-300 border-purple-800' : ''}
                            ${stage.type === 'filter' ? 'bg-zinc-800 text-zinc-300 border-zinc-700' : ''}
                            ${stage.type === 'similarity' ? 'bg-amber-900/30 text-amber-300 border-amber-800' : ''}
                        `}
                        title={JSON.stringify(stage.filter, null, 2)}
                        onClick={() => setEditingIndex(index)}
                    >
                        <span className="font-medium">{stage.description}</span>
                        <button
                            onClick={(e) => {
                                e.stopPropagation();
                                onRemoveStage(index);
                            }}
                            className="hover:text-white hover:bg-white/10 rounded-full p-0.5 transition-colors"
                        >
                            <Cross2Icon className="w-3 h-3" />
                        </button>
                        {index < stages.length - 1 && (
                            <span className="text-zinc-600 ml-1">→</span>
                        )}
                    </div>

                    {editingIndex === index && (
                        <FilterStageEditor
                            stage={stage}
                            onSave={(newStage) => {
                                onUpdateStage(index, newStage);
                                setEditingIndex(null);
                            }}
                            onCancel={() => setEditingIndex(null)}
                            datasetId={datasetId}
                            schema={schema}
                        />
                    )}
                </div>
            ))}

            {stages.length > 0 && (
                <button
                    onClick={onClearAll}
                    className="ml-2 text-xs text-zinc-500 hover:text-zinc-300 underline transition-colors"
                >
                    Clear All
                </button>
            )}
        </div>
    );
}
