'use client';

import { useState } from 'react';
import type { FieldColorConfig } from '@/lib/api';
import { classColor } from './MaskRenderer';
import { ARGMAX, sameRule, type ClassRule, type ProbabilityView, type ThresholdRule } from '@/lib/class-rules';

interface Props {
    fieldName: string;
    classes: number;
    maskTargets?: Record<string, string>;
    colorConfig?: FieldColorConfig;
    view: ProbabilityView;
    /** The field's saved default, which the view starts from. */
    savedRule: ClassRule;
    onChange: (view: ProbabilityView) => void;
    /** Present when the user may save the shown rule as the field's default. */
    onSave?: (rule: ClassRule) => Promise<void>;
}

/**
 * How a class-probability field is shown in the sample view: its class map from a rule, or one class's probability.
 * Editing the rule redraws the image at once and writes nothing; "Save as default" stores it for everyone.
 */
export function ClassRuleEditor({ fieldName, classes, maskTargets, colorConfig, view, savedRule, onChange, onSave }: Props) {
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const name = (k: number) => maskTargets?.[String(k)] ?? `class ${k}`;
    const swatch = (k: number) => `rgb(${classColor(k, colorConfig, maskTargets).join(',')})`;
    const indices = Array.from({ length: classes }, (_, k) => k);
    const rule = view.rule;
    const rules: ThresholdRule[] = rule.type === 'rules' ? rule.rules : [];
    const fallback = rule.type === 'rules' ? rule.fallback : null;
    const setRule = (next: ClassRule) => onChange({ ...view, rule: next });
    const setRules = (next: ThresholdRule[], nextFallback = fallback) =>
        setRule(next.length ? { type: 'rules', rules: next, fallback: nextFallback } : ARGMAX);
    const move = (from: number, to: number) => {
        const next = [...rules];
        const [entry] = next.splice(from, 1);
        next.splice(to, 0, entry);
        setRules(next);
    };
    const save = async () => {
        if (!onSave) return;
        setSaving(true);
        setError(null);
        try {
            await onSave(rule);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Saving the rule failed');
        } finally {
            setSaving(false);
        }
    };
    const select = 'bg-zinc-900 border border-zinc-700 rounded px-1.5 py-0.5 text-xs text-zinc-200';

    return (
        <div data-testid={`class-rule-editor-${fieldName}`} className="mt-1 ml-1 space-y-2 text-xs text-zinc-300">
            <label className="flex items-center gap-2">
                <span className="text-zinc-500 w-10">Show</span>
                <select
                    aria-label={`What ${fieldName} shows`}
                    className={select}
                    value={view.show === 'map' ? 'map' : String(view.show)}
                    onChange={e => onChange({ ...view, show: e.target.value === 'map' ? 'map' : Number(e.target.value) })}
                >
                    <option value="map">Class map</option>
                    {indices.map(k => <option key={k} value={k}>Probability of {name(k)}</option>)}
                </select>
            </label>
            {view.show === 'map' && (
                <>
                    <label className="flex items-center gap-2">
                        <span className="text-zinc-500 w-10">Rule</span>
                        <select
                            aria-label={`${fieldName} rule`}
                            className={select}
                            value={rule.type}
                            onChange={e => setRule(e.target.value === 'argmax'
                                ? ARGMAX
                                : { type: 'rules', rules: [{ classes: [1], min: 0.5, class: 1 }], fallback: null })}
                        >
                            <option value="argmax">Most probable class</option>
                            <option value="rules">Thresholds</option>
                        </select>
                    </label>
                    {rule.type === 'rules' && (
                        <div className="space-y-1.5">
                            <p className="text-[10px] text-zinc-500">The first threshold a pixel passes, from the top, gives its class.</p>
                            {rules.map((entry, index) => (
                                <div key={index} data-testid="class-threshold" className="flex items-center gap-1.5">
                                    <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: swatch(entry.class) }} />
                                    {entry.classes.length === 1 ? (
                                        <select
                                            aria-label={`Threshold ${index + 1} class`}
                                            className={select}
                                            value={entry.class}
                                            onChange={e => {
                                                const k = Number(e.target.value);
                                                setRules(rules.map((r, i) => (i === index ? { ...r, classes: [k], class: k } : r)));
                                            }}
                                        >
                                            {indices.map(k => <option key={k} value={k}>{name(k)}</option>)}
                                        </select>
                                    ) : (
                                        <span className="truncate" title="A threshold on these classes' summed probability">
                                            {entry.classes.map(name).join(' + ')} → {name(entry.class)}
                                        </span>
                                    )}
                                    <input
                                        type="range"
                                        aria-label={`Threshold ${index + 1} minimum`}
                                        min={0}
                                        max={1}
                                        step={0.01}
                                        value={entry.min}
                                        onChange={e => setRules(rules.map((r, i) => (i === index ? { ...r, min: Number(e.target.value) } : r)))}
                                        className="flex-1 min-w-0 accent-cyan-500"
                                    />
                                    <span className="w-8 text-right tabular-nums">{entry.min.toFixed(2)}</span>
                                    <button aria-label="Move up" disabled={index === 0} onClick={() => move(index, index - 1)}
                                        className="px-0.5 text-zinc-500 hover:text-zinc-200 disabled:opacity-30">↑</button>
                                    <button aria-label="Move down" disabled={index === rules.length - 1} onClick={() => move(index, index + 1)}
                                        className="px-0.5 text-zinc-500 hover:text-zinc-200 disabled:opacity-30">↓</button>
                                    <button aria-label="Remove threshold" onClick={() => setRules(rules.filter((_, i) => i !== index))}
                                        className="px-0.5 text-zinc-500 hover:text-red-300">×</button>
                                </div>
                            ))}
                            <button
                                onClick={() => {
                                    const unused = indices.find(k => k > 0 && !rules.some(r => r.class === k)) ?? 1;
                                    setRules([...rules, { classes: [unused], min: 0.5, class: unused }]);
                                }}
                                className="text-cyan-400 hover:text-cyan-300"
                            >
                                + Add a threshold
                            </button>
                            <label className="flex items-center gap-2">
                                <span className="text-zinc-500">Otherwise</span>
                                <select
                                    aria-label={`${fieldName} fallback`}
                                    className={select}
                                    value={fallback === null ? '' : String(fallback)}
                                    onChange={e => setRules(rules, e.target.value === '' ? null : Number(e.target.value))}
                                >
                                    <option value="">nothing</option>
                                    {indices.filter(k => k > 0).map(k => <option key={k} value={k}>{name(k)}</option>)}
                                </select>
                            </label>
                        </div>
                    )}
                    <div className="flex items-center gap-2">
                        {!sameRule(rule, savedRule) && (
                            <button onClick={() => setRule(savedRule)} className="text-zinc-400 hover:text-white">
                                Back to the default
                            </button>
                        )}
                        {onSave && !sameRule(rule, savedRule) && (
                            <button
                                onClick={save}
                                disabled={saving}
                                className="px-2 py-0.5 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white"
                            >
                                {saving ? 'Saving…' : 'Save as default'}
                            </button>
                        )}
                    </div>
                    {error && <p className="text-red-300">{error}</p>}
                </>
            )}
        </div>
    );
}
