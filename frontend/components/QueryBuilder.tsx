import React from 'react';
import { ComplexFilter, SchemaField } from '../lib/api';

interface QueryBuilderProps {
    schema: SchemaField[];
    value: ComplexFilter;
    onChange: (filter: ComplexFilter) => void;
}

export default function QueryBuilder({ schema, value, onChange }: QueryBuilderProps) {
    // Helper to create a new empty leaf filter
    const createLeaf = (): ComplexFilter => ({ field: schema[0]?.name || '', operator: '=', value: '' });

    // Helper to create a new group
    const createGroup = (logic: 'AND' | 'OR'): ComplexFilter => ({
        logic,
        filters: [createLeaf(), createLeaf()]
    });

    function handleLogicChange(newLogic: 'AND' | 'OR' | 'NOT') {
        onChange({ ...value, logic: newLogic });
    }

    function addFilter() {
        const newFilters = [...(value.filters || []), createLeaf()];
        onChange({ ...value, filters: newFilters });
    }

    function addGroup() {
        const newFilters = [...(value.filters || []), createGroup('AND')];
        onChange({ ...value, filters: newFilters });
    }

    function removeFilter(index: number) {
        const newFilters = [...(value.filters || [])];
        newFilters.splice(index, 1);
        onChange({ ...value, filters: newFilters });
    }

    function updateFilter(index: number, newValue: ComplexFilter) {
        const newFilters = [...(value.filters || [])];
        newFilters[index] = newValue;
        onChange({ ...value, filters: newFilters });
    }

    // Render Logic Group (AND/OR/NOT)
    if (value.logic) {
        return (
            <div className="border border-gray-300 dark:border-gray-600 rounded p-3 bg-gray-50 dark:bg-gray-800/50">
                <div className="flex items-center gap-2 mb-2">
                    <select
                        value={value.logic}
                        onChange={e => handleLogicChange(e.target.value as 'AND' | 'OR' | 'NOT')}
                        className="bg-cyan-100 dark:bg-cyan-900 text-cyan-800 dark:text-cyan-100 font-bold rounded px-2 py-1 text-sm border-none"
                    >
                        <option value="AND">AND</option>
                        <option value="OR">OR</option>
                        <option value="NOT">NOT</option>
                    </select>
                    <div className="flex-1"></div>
                    <button onClick={addFilter} className="text-xs bg-gray-200 dark:bg-gray-700 px-2 py-1 rounded hover:bg-gray-300">+ Rule</button>
                    <button onClick={addGroup} className="text-xs bg-gray-200 dark:bg-gray-700 px-2 py-1 rounded hover:bg-gray-300">+ Group</button>
                </div>

                <div className="space-y-2 pl-2 border-l-2 border-gray-200 dark:border-gray-700">
                    {value.filters?.map((f, i) => (
                        <div key={i} className="flex items-start gap-2">
                            <div className="flex-1">
                                <QueryBuilder
                                    schema={schema}
                                    value={f}
                                    onChange={newVal => updateFilter(i, newVal)}
                                />
                            </div>
                            <button
                                onClick={() => removeFilter(i)}
                                className="text-red-500 hover:bg-red-100 dark:hover:bg-red-900/30 rounded px-2 py-1"
                            >
                                ×
                            </button>
                        </div>
                    ))}
                </div>
            </div>
        );
    }

    // Render Leaf Node (Field Condition)
    const fieldSchema = schema.find(s => s.name === value.field);
    const operators = ['=', '!=', '>', '<', '>=', '<=', 'contains', 'in', 'not in'];

    return (
        <div className="flex items-center gap-2 bg-white dark:bg-gray-800 p-2 rounded border border-gray-200 dark:border-gray-700">
            <select
                value={value.field}
                onChange={e => onChange({ ...value, field: e.target.value, value: '' })}
                className="p-1 text-sm border rounded dark:bg-gray-700 dark:text-white max-w-[120px]"
            >
                {schema.map(s => (
                    <option key={s.name} value={s.name}>{s.name}</option>
                ))}
            </select>

            <select
                value={value.operator}
                onChange={e => onChange({ ...value, operator: e.target.value as ComplexFilter['operator'] })}
                className="p-1 text-sm border rounded dark:bg-gray-700 dark:text-white w-20"
            >
                {operators.map(op => (
                    <option key={op} value={op}>{op}</option>
                ))}
            </select>

            <div className="flex-1">
                {fieldSchema?.type === 'boolean' ? (
                    <select
                        value={value.value}
                        onChange={e => onChange({ ...value, value: e.target.value === 'true' })}
                        className="w-full p-1 text-sm border rounded dark:bg-gray-700 dark:text-white"
                    >
                        <option value="true">True</option>
                        <option value="false">False</option>
                    </select>
                ) : fieldSchema?.type === 'enum' ? (
                    <select
                        value={value.value}
                        onChange={e => onChange({ ...value, value: e.target.value })}
                        className="w-full p-1 text-sm border rounded dark:bg-gray-700 dark:text-white"
                    >
                        <option value="">Select...</option>
                        {fieldSchema.values?.map(v => (
                            <option key={v} value={v}>{v}</option>
                        ))}
                    </select>
                ) : (
                    <input
                        type="text"
                        value={String(value.value || '')}
                        onChange={e => onChange({ ...value, value: e.target.value })}
                        placeholder="Value"
                        className="w-full p-1 text-sm border rounded dark:bg-gray-700 dark:text-white"
                    />
                )}
            </div>
        </div>
    );
}
