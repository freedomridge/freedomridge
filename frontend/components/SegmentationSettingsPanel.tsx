'use client';

import React, { useState } from 'react';
import { SegmentationConfig } from '@/lib/api';
import { getOverlayColorConfig } from '@/lib/labels';
import { generateColor } from '@/lib/annotation-utils';

function getDefaultColor(label: string): string {
    return generateColor(label);
}

// Generate a default color based on class ID (0-255)
function getDefaultColorForId(id: string | number): string {
    const numId = typeof id === 'string' ? parseInt(id) : id;
    // Use a color palette that looks good for segmentation
    const palette = [
        '#e6194B', '#3cb44b', '#ffe119', '#4363d8', '#f58231',
        '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#fabed4',
        '#469990', '#dcbeff', '#9A6324', '#fffac8', '#800000',
        '#aaffc3', '#808000', '#ffd8b1', '#000075', '#a9a9a9'
    ];
    return palette[numId % palette.length] || '#888888';
}

export interface SegmentationSettingsPanelProps {
    config: SegmentationConfig | undefined;
    onChange: (config: SegmentationConfig) => void;
    /** List of label values from the dataset schema */
    labelFields: { name: string; labels: string[] }[];
    /** List of segmentation mask label values (from mask_targets) */
    availableSegmentationLabels?: string[];
    /** If true, all fields are read-only */
    readOnly?: boolean;
}

export function SegmentationSettingsPanel({
    config,
    onChange,
    labelFields,
    availableSegmentationLabels = [],
    readOnly = false
}: SegmentationSettingsPanelProps) {
    // Get current opacity (default 40%)
    const globalOpacity = config?.color_scheme?.opacity ?? 0.4;

    // Update global opacity
    const updateOpacity = (opacity: number) => {
        const currentConfig = config || {};
        const colorScheme = currentConfig.color_scheme || {};

        onChange({
            ...currentConfig,
            color_scheme: {
                ...colorScheme,
                opacity
            }
        });
    };

    // Update color for a specific label in a specific field
    const updateLabelColor = (field: string, label: string, color: string) => {
        const currentConfig = config || {};
        const colorScheme = currentConfig.color_scheme || {};
        const fields = colorScheme.fields || {};
        const fieldConfig = fields[field] ?? fields[`${field}.label`] ?? { colorByAttribute: 'label', valueColors: {} };
        const valueColors = fieldConfig.valueColors || {};

        onChange({
            ...currentConfig,
            color_scheme: {
                ...colorScheme,
                fields: {
                    ...fields,
                    [field]: {
                        ...fieldConfig,
                        valueColors: {
                            ...valueColors,
                            [label]: color
                        }
                    }
                }
            }
        });
    };

    // Reset a label to default color
    const resetLabelColor = (field: string, label: string) => {
        const currentConfig = config || {};
        const colorScheme = currentConfig.color_scheme || {};
        const fields = colorScheme.fields || {};
        const fieldConfig = fields[field] ?? fields[`${field}.label`] ?? { colorByAttribute: 'label', valueColors: {} };
        const valueColors = { ...fieldConfig.valueColors };
        delete valueColors[label];

        onChange({
            ...currentConfig,
            color_scheme: {
                ...colorScheme,
                fields: {
                    ...fields,
                    [field]: {
                        ...fieldConfig,
                        valueColors
                    }
                }
            }
        });
    };

    // ===== Mask Targets Management =====
    // Get current mask targets for segmentation field
    const getMaskTargets = (fieldName: string = 'segmentation'): Record<string, string> => {
        return config?.mask_targets?.[fieldName] || config?.default_mask_targets || {};
    };

    const maskTargets = getMaskTargets('segmentation');

    // Update a mask target label
    const updateMaskTarget = (id: string, label: string) => {
        const currentConfig = config || {};
        const currentTargets = currentConfig.mask_targets || {};
        const fieldTargets = currentTargets['segmentation'] || currentConfig.default_mask_targets || {};

        onChange({
            ...currentConfig,
            mask_targets: {
                ...currentTargets,
                segmentation: {
                    ...fieldTargets,
                    [id]: label
                }
            }
        });
    };

    // Remove a mask target
    const removeMaskTarget = (id: string) => {
        const currentConfig = config || {};
        const currentTargets = currentConfig.mask_targets || {};
        const fieldTargets = { ...(currentTargets['segmentation'] || currentConfig.default_mask_targets || {}) };
        delete fieldTargets[id];

        onChange({
            ...currentConfig,
            mask_targets: {
                ...currentTargets,
                segmentation: fieldTargets
            }
        });
    };

    // State for adding new mapping
    const [newMappingId, setNewMappingId] = useState('');
    const [newMappingLabel, setNewMappingLabel] = useState('');

    // Add a new mask target
    const addMaskTarget = () => {
        if (!newMappingId || !newMappingLabel) return;
        updateMaskTarget(newMappingId, newMappingLabel);
        setNewMappingId('');
        setNewMappingLabel('');
    };

    // ===== Mask Color Management (keyed by ID, not label) =====
    // Get mask colors keyed by ID
    const getMaskColors = (): Record<string, string> => {
        return config?.color_scheme?.fields?.segmentation?.idColors || {};
    };

    const maskColors = getMaskColors();

    // Get color for a mask ID (with fallback to default)
    const getMaskColor = (id: string): string => {
        return maskColors[id] || getDefaultColorForId(id);
    };

    // Update color for a mask ID
    const updateMaskColor = (id: string, color: string) => {
        const currentConfig = config || {};
        const colorScheme = currentConfig.color_scheme || {};
        const fields = colorScheme.fields || {};
        const segConfig = fields.segmentation || { colorByAttribute: 'label', valueColors: {} };
        const idColors = segConfig.idColors || {};

        onChange({
            ...currentConfig,
            color_scheme: {
                ...colorScheme,
                fields: {
                    ...fields,
                    segmentation: {
                        ...segConfig,
                        idColors: {
                            ...idColors,
                            [id]: color
                        }
                    }
                }
            }
        });
    };

    // Check if we have mask targets to show
    const hasMaskTargets = Object.keys(maskTargets).length > 0;

    if (labelFields.length === 0 && availableSegmentationLabels.length === 0 && !hasMaskTargets) {
        return (
            <div className="text-sm text-zinc-500 italic py-4">
                No configurable labels found in this dataset.
            </div>
        );
    }

    return (
        <div className="space-y-8">
            {/* Global Opacity Slider */}
            <div>
                <label className="block text-sm font-medium text-zinc-400 mb-2">
                    Overlay Opacity: {Math.round(globalOpacity * 100)}%
                </label>
                <input
                    type="range"
                    min="0"
                    max="100"
                    value={globalOpacity * 100}
                    onChange={(e) => updateOpacity(Number(e.target.value) / 100)}
                    disabled={readOnly}
                    className={`w-full h-2 bg-zinc-700 rounded-lg appearance-none cursor-pointer accent-cyan-500 ${readOnly ? 'opacity-60' : ''}`}
                />
            </div>

            {labelFields.map(field => (
                <div key={field.name} data-testid={`overlay-colors-${field.name}`} className="space-y-4">
                    <LabelColorGroup
                        title={field.name}
                        labels={field.labels}
                        customColors={getOverlayColorConfig(config, field.name)?.valueColors || {}}
                        onColorChange={(label, color) => updateLabelColor(field.name, label, color)}
                        onReset={label => resetLabelColor(field.name, label)}
                        readOnly={readOnly}
                    />
                </div>
            ))}

            {/* Mask Targets Editor Section */}
            <div className="space-y-4">
                <h3 className="text-sm font-medium text-white border-b border-zinc-800 pb-2">
                    Mask Class Mappings
                </h3>
                <p className="text-xs text-zinc-500">
                    Define what each class ID in your mask images represents.
                </p>

                {/* Existing mappings */}
                <div className="space-y-2">
                    {Object.entries(maskTargets)
                        .sort(([a], [b]) => parseInt(a) - parseInt(b))
                        .map(([id, label]) => (
                            <div key={id} className="flex items-center gap-2 group">
                                <span className="text-sm text-zinc-500 w-12">ID {id}</span>
                                <span className="text-zinc-600">→</span>
                                <input
                                    type="text"
                                    value={label}
                                    onChange={(e) => updateMaskTarget(id, e.target.value)}
                                    disabled={readOnly}
                                    className={`flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-zinc-500 ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
                                    placeholder="Label name"
                                />
                                <input
                                    type="color"
                                    value={getMaskColor(id)}
                                    onChange={(e) => updateMaskColor(id, e.target.value)}
                                    disabled={readOnly}
                                    className={`w-6 h-6 rounded border-0 cursor-pointer bg-transparent ${readOnly ? 'opacity-60' : ''}`}
                                    title={`Color for ID ${id}`}
                                />
                                {!readOnly && (
                                    <button
                                        onClick={() => removeMaskTarget(id)}
                                        className="text-zinc-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity px-1"
                                        title="Remove mapping"
                                    >
                                        ×
                                    </button>
                                )}
                            </div>
                        ))}
                </div>

                {/* Add new mapping - only show if not read-only */}
                {!readOnly && (
                    <div className="flex items-center gap-2 pt-2 border-t border-zinc-800">
                        <span className="text-sm text-zinc-500 w-12">ID</span>
                        <input
                            type="number"
                            min="0"
                            max="255"
                            value={newMappingId}
                            onChange={(e) => setNewMappingId(e.target.value)}
                            className="w-16 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-zinc-500"
                            placeholder="#"
                        />
                        <span className="text-zinc-600">→</span>
                        <input
                            type="text"
                            value={newMappingLabel}
                            onChange={(e) => setNewMappingLabel(e.target.value)}
                            className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-zinc-500"
                            placeholder="Label name"
                            onKeyDown={(e) => e.key === 'Enter' && addMaskTarget()}
                        />
                        <button
                            onClick={addMaskTarget}
                            disabled={!newMappingId || !newMappingLabel}
                            className="px-3 py-1 bg-zinc-700 hover:bg-zinc-600 text-white text-sm rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            Add
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

interface LabelColorGroupProps {
    title: string;
    labels: string[];
    customColors: Record<string, string>;
    onColorChange: (label: string, color: string) => void;
    onReset: (label: string) => void;
    readOnly?: boolean;
}

function LabelColorGroup({ title, labels, customColors, onColorChange, onReset, readOnly = false }: LabelColorGroupProps) {
    return (
        <div className="mb-4">
            <h4 className="text-xs text-zinc-500 uppercase tracking-wide mb-2">{title}</h4>
            <div className="space-y-1">
                {labels.map(label => {
                    const hasCustomColor = label in customColors;
                    const displayColor = customColors[label] || getDefaultColor(label);
                    const shortLabel = label.split('/')[1] || label;

                    return (
                        <div key={label} className="flex items-center gap-2 group">
                            <input
                                type="color"
                                value={displayColor}
                                onChange={(e) => onColorChange(label, e.target.value)}
                                disabled={readOnly}
                                className={`w-6 h-6 rounded border-0 bg-transparent ${readOnly ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer'}`}
                                title={`Color for ${label}`}
                            />
                            <span
                                className="flex-1 text-sm text-zinc-300 truncate"
                                title={label}
                            >
                                {shortLabel}
                            </span>
                            {hasCustomColor && !readOnly && (
                                <button
                                    onClick={() => onReset(label)}
                                    className="text-xs text-zinc-500 hover:text-zinc-300 opacity-0 group-hover:opacity-100 transition-opacity"
                                    title="Reset to default color"
                                >
                                    Reset
                                </button>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

export default SegmentationSettingsPanel;
