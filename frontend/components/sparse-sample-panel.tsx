'use client';

import { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { getSparseSampleFields, sparseSample, type SparseSampleField, type ComplexFilter } from '@/lib/api';
import { Crosshair2Icon, CheckIcon } from '@radix-ui/react-icons';

interface SparseSamplePanelProps {
    datasetId: number;
    filters: ComplexFilter;
    onSelection: (ids: number[]) => void;
}

export function SparseSamplePanel({ datasetId, filters, onSelection }: SparseSamplePanelProps) {
    const [selectedFields, setSelectedFields] = useState<Set<string>>(new Set());
    const [weights, setWeights] = useState<Record<string, number>>({});
    const [mode, setMode] = useState<'count' | 'percentage'>('count');
    const [count, setCount] = useState(100);
    const [percentage, setPercentage] = useState(10);
    const [result, setResult] = useState<{ count: number; candidateCount: number; elapsed: number } | null>(null);

    const { data: availableFields, isLoading } = useQuery({
        queryKey: ['sparse-sample-fields', datasetId],
        queryFn: () => getSparseSampleFields(datasetId),
        staleTime: 60_000,
    });

    const mutation = useMutation({
        mutationFn: () => {
            const fields = Array.from(selectedFields);
            const w = Object.keys(weights).length > 0 ? weights : undefined;
            return sparseSample(datasetId, fields, {
                count: mode === 'count' ? count : undefined,
                percentage: mode === 'percentage' ? percentage : undefined,
                weights: w,
                filters,
            });
        },
        onSuccess: (data) => {
            setResult({ count: data.count, candidateCount: data.candidate_count, elapsed: data.elapsed_seconds });
            onSelection(data.sample_ids);
        },
    });

    const toggleField = (name: string) => {
        setSelectedFields(prev => {
            const next = new Set(prev);
            if (next.has(name)) {
                next.delete(name);
                const w = { ...weights };
                delete w[name];
                setWeights(w);
            } else {
                next.add(name);
            }
            return next;
        });
        setResult(null);
    };

    const updateWeight = (name: string, value: number) => {
        setWeights(prev => ({ ...prev, [name]: value }));
    };

    const typeIcon = (type: string) => {
        switch (type) {
            case 'location': return '📍';
            case 'embedding': return '🧬';
            case 'numeric': return '#';
            default: return '·';
        }
    };

    return (
        <div className="flex flex-col h-full p-4 gap-4 overflow-y-auto">
            {/* Header */}
            <div>
                <h3 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
                    <Crosshair2Icon className="w-4 h-4 text-cyan-400" />
                    Sparse Sample
                </h3>
                <p className="text-[11px] text-zinc-500 mt-1">
                    Select a maximally diverse subset from your current view.
                </p>
            </div>

            {/* Fields */}
            <div>
                <label className="text-[11px] text-zinc-400 font-medium uppercase tracking-wider">Diversify On</label>
                <div className="mt-2 flex flex-col gap-1.5">
                    {isLoading && <div className="text-xs text-zinc-600">Loading fields...</div>}
                    {availableFields?.map((field: SparseSampleField) => {
                        const isSelected = selectedFields.has(field.name);
                        return (
                            <div key={field.name} className="flex items-center gap-2">
                                <button
                                    onClick={() => toggleField(field.name)}
                                    className={`flex-1 flex items-center gap-2 px-3 py-2 rounded text-xs text-left transition-all border ${
                                        isSelected
                                            ? 'bg-cyan-500/10 border-cyan-500/40 text-cyan-200'
                                            : 'bg-zinc-800/50 border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-300'
                                    }`}
                                >
                                    <span className="w-5 text-center">{isSelected ? <CheckIcon className="w-3.5 h-3.5 inline" /> : typeIcon(field.type)}</span>
                                    <span className="font-medium">{field.name}</span>
                                    <span className="text-[10px] text-zinc-600 ml-auto">{field.description}</span>
                                </button>
                                {isSelected && (
                                    <div className="flex items-center gap-1">
                                        <label className="text-[10px] text-zinc-600">wt</label>
                                        <input
                                            type="number"
                                            min={0.1}
                                            max={10}
                                            step={0.1}
                                            value={weights[field.name] ?? 1.0}
                                            onChange={(e) => updateWeight(field.name, parseFloat(e.target.value) || 1.0)}
                                            className="w-14 bg-zinc-800 border border-zinc-700 rounded px-1.5 py-1 text-[11px] text-zinc-300 text-center focus:outline-none focus:border-cyan-500"
                                        />
                                    </div>
                                )}
                            </div>
                        );
                    })}
                    {availableFields && availableFields.length === 0 && (
                        <div className="text-xs text-zinc-600 italic py-2">
                            No fields available. Compute embeddings or add location data first.
                        </div>
                    )}
                </div>
            </div>

            {/* Sample Size */}
            <div>
                <label className="text-[11px] text-zinc-400 font-medium uppercase tracking-wider">Sample Size</label>
                <div className="mt-2 flex items-center gap-2">
                    <div className="flex bg-zinc-800 rounded p-0.5 border border-zinc-700">
                        <button
                            onClick={() => setMode('count')}
                            className={`text-[11px] px-3 py-1 rounded ${mode === 'count' ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
                        >
                            Count
                        </button>
                        <button
                            onClick={() => setMode('percentage')}
                            className={`text-[11px] px-3 py-1 rounded ${mode === 'percentage' ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
                        >
                            Percent
                        </button>
                    </div>
                    {mode === 'count' ? (
                        <input
                            type="number"
                            min={1}
                            value={count}
                            onChange={(e) => { setCount(parseInt(e.target.value) || 1); setResult(null); }}
                            className="w-24 bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-cyan-500"
                        />
                    ) : (
                        <div className="flex items-center gap-1">
                            <input
                                type="number"
                                min={0.1}
                                max={100}
                                step={0.1}
                                value={percentage}
                                onChange={(e) => { setPercentage(parseFloat(e.target.value) || 1); setResult(null); }}
                                className="w-20 bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-cyan-500"
                            />
                            <span className="text-xs text-zinc-500">%</span>
                        </div>
                    )}
                </div>
            </div>

            {/* Run Button */}
            <button
                onClick={() => mutation.mutate()}
                disabled={selectedFields.size === 0 || mutation.isPending}
                className={`w-full py-2.5 rounded font-medium text-sm transition-all ${
                    selectedFields.size === 0
                        ? 'bg-zinc-800 text-zinc-600 cursor-not-allowed'
                        : mutation.isPending
                            ? 'bg-cyan-700 text-cyan-200 cursor-wait'
                            : 'bg-cyan-600 text-white hover:bg-cyan-500 shadow-lg shadow-cyan-500/10'
                }`}
            >
                {mutation.isPending ? 'Sampling...' : 'Sample'}
            </button>

            {/* Error */}
            {mutation.isError && (
                <div className="bg-red-500/10 border border-red-500/30 rounded px-3 py-2 text-xs text-red-300">
                    {(mutation.error as Error).message}
                </div>
            )}

            {/* Result */}
            {result && (result.candidateCount === 0 ? (
                <div className="bg-amber-500/10 border border-amber-500/30 rounded px-3 py-2 text-xs text-amber-300">
                    No samples in the current view have the selected field{selectedFields.size > 1 ? 's' : ''}.
                    Try a different field or widen the filters.
                </div>
            ) : (
                <div className="bg-cyan-500/10 border border-cyan-500/20 rounded px-3 py-3">
                    <div className="text-xs text-cyan-200 font-medium">
                        Selected {result.count.toLocaleString()} samples from {result.candidateCount.toLocaleString()} candidates
                    </div>
                    <div className="text-[10px] text-zinc-500 mt-1">
                        {result.elapsed}s &middot; Applied as selection
                    </div>
                </div>
            ))}

            {/* Info */}
            <div className="mt-auto text-[10px] text-zinc-600 leading-relaxed">
                Uses farthest-point sampling to maximize diversity across selected fields.
                Each field is normalized to equal scale. Adjust weights to prioritize specific fields.
            </div>
        </div>
    );
}
