'use client';

import { useState, useEffect, useRef } from 'react';
import { Cross2Icon } from '@radix-ui/react-icons';
import { useQueryClient } from '@tanstack/react-query';
import { useToast } from '@/contexts/ToastContext';
import { useFilters } from '@/contexts/FilterContext';

// From the tag registry. created_at/created_by null = unknown: the tag predates
// the registry or arrived through a path that doesn't register (ingest, SDK field writes).
type TagInfo = { created_at: string | null; created_by: string | null; last_used_at: string | null };

interface TagState {
    tag: string;
    count: number;
    total: number;
    checked: boolean;
    indeterminate: boolean;
    info?: TagInfo;
}

interface TagManagementModalProps {
    selectedSampleIds: number[];
    // Select-All mode: tag by a server-side token (+ exceptions) instead of an id list.
    selectionToken?: string | null;
    excludedIds?: number[];
    selectionCount?: number;
    datasetId: number;
    fieldName?: string;  // Which array field to manage (default: 'tags')
    onClose: () => void;
    onUpdate: () => void;
}

export function TagManagementModal({
    selectedSampleIds,
    selectionToken = null,
    excludedIds = [],
    selectionCount,
    datasetId,
    fieldName = 'tags',
    onClose,
    onUpdate
}: TagManagementModalProps) {
    const queryClient = useQueryClient();
    const { addToast } = useToast();
    const [tags, setTags] = useState<TagState[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const { clearSelection } = useFilters();
    const [error, setError] = useState<string | null>(null);
    const [newTagName, setNewTagName] = useState('');

    // Staged changes - track what user wants to add/remove
    const [pendingAdd, setPendingAdd] = useState<Set<string>>(new Set());
    const [pendingRemove, setPendingRemove] = useState<Set<string>>(new Set());

    // Selection payload: a token (+ exceptions) for Select-All, else the explicit id list.
    const selectionPayload = selectionToken
        ? { selection_token: selectionToken, excluded_ids: excludedIds }
        : { sample_ids: selectedSampleIds };
    // In token mode selectedSampleIds is empty, so use the count passed from the page.
    const effectiveCount = selectionCount ?? selectedSampleIds.length;

    async function loadTagCounts() {
        setLoading(true);
        setError(null);

        try {
            // POST body, not query string: large selections (10k+ ids) 414 in the URL.
            const res = await fetch(`/api/datasets/${datasetId}/samples/tag-counts`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...selectionPayload, field_name: fieldName })
            });

            if (!res.ok) {
                const detail = (await res.json().catch(() => ({}))).detail;
                if (res.status === 410) clearSelection();  // the Select-All token expired
                throw new Error(detail || `Failed to load tags: ${res.statusText}`);
            }

            const data = await res.json();

            const tagStates = data.all_tags.map((tag: string) => ({
                tag,
                count: data.tag_counts[tag],
                total: data.total_samples,
                checked: data.tag_counts[tag] === data.total_samples,
                indeterminate: data.tag_counts[tag] > 0 && data.tag_counts[tag] < data.total_samples,
                info: data.tag_info?.[tag],
            }));

            setTags(tagStates);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load tags');
        } finally {
            setLoading(false);
        }
    }

    // Keyed on the ids, not the arrays: the single-image view passes a new [id] on every render and the
    // excludedIds default is a new [] on every render, so array deps reloaded after every load (#10).
    const sampleKey = selectedSampleIds.join(',');
    const excludedKey = excludedIds.join(',');
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- reset request state when the tag target changes
        loadTagCounts();
        // eslint-disable-next-line react-hooks/exhaustive-deps -- reload tag counts only when the target samples/dataset change; loadTagCounts is recreated every render and adding it would refetch on every render
    }, [sampleKey, selectionToken, excludedKey, datasetId]);

    // Stage tag change (don't apply yet)
    function handleTagToggle(tag: string, newChecked: boolean) {
        if (newChecked) {
            // User wants to add this tag
            setPendingAdd(prev => new Set(prev).add(tag));
            setPendingRemove(prev => {
                const next = new Set(prev);
                next.delete(tag);  // Cancel any pending remove
                return next;
            });
        } else {
            // User wants to remove this tag
            setPendingRemove(prev => new Set(prev).add(tag));
            setPendingAdd(prev => {
                const next = new Set(prev);
                next.delete(tag);  // Cancel any pending add
                return next;
            });
        }
    }

    // Stage new tag creation
    function stageNewTag() {
        const tagName = newTagName.trim();

        if (!tagName) {
            setError('Tag name cannot be empty');
            return;
        }

        // Check if tag already exists
        if (tags.some(t => t.tag.toLowerCase() === tagName.toLowerCase())) {
            setError('Tag already exists');
            return;
        }

        // Add to tags list as unchecked and stage for addition
        setTags(prev => [...prev, {
            tag: tagName,
            count: 0,
            total: effectiveCount,
            checked: false,
            indeterminate: false
        }]);

        setPendingAdd(prev => new Set(prev).add(tagName));
        setNewTagName('');
        setError(null);
    }

    // Apply all staged changes
    async function applyChanges() {
        const tagsToAdd = Array.from(pendingAdd);
        const tagsToRemove = Array.from(pendingRemove);

        if (tagsToAdd.length === 0 && tagsToRemove.length === 0) {
            // No changes, just close
            onClose();
            return;
        }

        setSaving(true);
        setError(null);

        try {
            const res = await fetch(`/api/datasets/${datasetId}/samples/bulk-tag`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',  // Send cookies for authentication
                body: JSON.stringify({
                    ...selectionPayload,
                    add_tags: tagsToAdd,
                    remove_tags: tagsToRemove,
                    field_name: fieldName
                })
            });

            if (!res.ok) {
                if (res.status === 401) {
                    // Session expired — this raw fetch bypasses the api.ts client,
                    // so fire the global redirect-to-login event ourselves instead
                    // of throwing into the void.
                    if (typeof window !== 'undefined') {
                        window.dispatchEvent(new Event('fridge:unauthorized'));
                    }
                    setError('Your session has expired. Please log in again.');
                    setSaving(false);
                    return;
                }
                if (res.status === 403) {
                    setError('Permission denied. You need "Power User" or "Admin" role to tag samples.');
                    setSaving(false);
                    return;
                }
                const errorData = await res.json().catch(() => ({}));
                if (res.status === 410) clearSelection();  // the Select-All token expired
                throw new Error(errorData.detail || `Failed to update tags: ${res.statusText}`);
            }

            if (res.status === 202) {
                queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', datasetId] });
                queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
                addToast('Bulk edit queued. Follow progress in Jobs; cancelling keeps completed chunks.', 'info', 6000);
            } else {
                // On a mirror (docs/FOLLOWERS.md) the edit was sent to its primary: say so, and when it shows here.
                const result = await res.json().catch(() => ({}));
                if (result.forwarded_to) addToast(result.message, 'info', 8000);
            }
            // Success - notify parent and close
            onUpdate();
            onClose();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to apply changes');
            setSaving(false);
        }
    }

    // Get effective checked state considering pending changes
    function getEffectiveState(tagState: TagState) {
        const { tag, checked, indeterminate } = tagState;

        if (pendingAdd.has(tag)) {
            return { checked: true, indeterminate: false };
        }
        if (pendingRemove.has(tag)) {
            return { checked: false, indeterminate: false };
        }
        return { checked, indeterminate };
    }

    const hasChanges = pendingAdd.size > 0 || pendingRemove.size > 0;

    return (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full max-h-[80vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
                    <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                        {fieldName === 'tags' ? 'Tag' : fieldName} — {effectiveCount.toLocaleString()} Sample{effectiveCount !== 1 ? 's' : ''}
                    </h2>
                    <button
                        onClick={onClose}
                        className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                        disabled={saving}
                    >
                        <Cross2Icon className="w-5 h-5" />
                    </button>
                </div>

                {/* Content */}
                <div className="flex-1 overflow-y-auto p-4">
                    {/* New Tag Input */}
                    <div className="mb-4 pb-4 border-b border-gray-200 dark:border-gray-700">
                        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                            Create New Tag
                        </label>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                value={newTagName}
                                onChange={(e) => {
                                    setNewTagName(e.target.value);
                                    setError(null);
                                }}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter') {
                                        e.preventDefault();
                                        stageNewTag();
                                    } else if (e.key === 'Escape') {
                                        setNewTagName('');
                                        setError(null);
                                    }
                                }}
                                placeholder="Enter tag name..."
                                disabled={saving}
                                className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-cyan-500 focus:border-transparent disabled:opacity-50"
                            />
                            <button
                                onClick={stageNewTag}
                                disabled={saving || !newTagName.trim()}
                                className="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:bg-gray-400 text-white rounded-md font-medium transition-colors disabled:cursor-not-allowed"
                            >
                                Add
                            </button>
                        </div>
                        {error && (
                            <p className="mt-2 text-sm text-red-600 dark:text-red-400">
                                {error}
                            </p>
                        )}
                    </div>

                    {/* Existing Tags List */}
                    {loading ? (
                        <div className="text-center py-8 text-gray-500">
                            Loading tags...
                        </div>
                    ) : tags.length === 0 && !newTagName.trim() ? (
                        <div className="text-center py-8 text-gray-500">
                            No tags in this dataset
                        </div>
                    ) : (
                        <div className="space-y-2">
                            {tags.map((tagState) => {
                                const { checked, indeterminate } = getEffectiveState(tagState);
                                const isPending = pendingAdd.has(tagState.tag) || pendingRemove.has(tagState.tag);

                                return (
                                    <TagCheckboxItem
                                        key={tagState.tag}
                                        tag={tagState.tag}
                                        count={tagState.count}
                                        total={tagState.total}
                                        checked={checked}
                                        indeterminate={indeterminate}
                                        isPending={isPending}
                                        info={tagState.info}
                                        onChange={(newChecked) => handleTagToggle(tagState.tag, newChecked)}
                                        disabled={saving}
                                    />
                                );
                            })}
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex justify-between items-center">
                    <div className="text-xs text-gray-600 dark:text-gray-400 max-w-[250px]">
                        {pendingAdd.size > 0 && (
                            <div className="text-green-600 dark:text-green-400 truncate" title={[...pendingAdd].join(', ')}>
                                +{effectiveCount.toLocaleString()} {[...pendingAdd].join(', ')}
                            </div>
                        )}
                        {pendingRemove.size > 0 && (
                            <div className="text-red-600 dark:text-red-400 truncate" title={[...pendingRemove].join(', ')}>
                                -{effectiveCount.toLocaleString()} {[...pendingRemove].join(', ')}
                            </div>
                        )}
                    </div>
                    <div className="flex gap-2">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md"
                            disabled={saving}
                        >
                            Cancel
                        </button>
                        <button
                            onClick={applyChanges}
                            disabled={saving || !hasChanges}
                            className="px-4 py-2 text-sm font-medium text-white bg-cyan-600 hover:bg-cyan-700 disabled:bg-gray-400 rounded-md disabled:cursor-not-allowed"
                        >
                            {saving ? 'Applying...' : 'Apply Changes'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

interface TagCheckboxItemProps {
    tag: string;
    count: number;
    total: number;
    checked: boolean;
    info?: TagInfo;
    indeterminate: boolean;
    isPending: boolean;
    onChange: (checked: boolean) => void;
    disabled: boolean;
}

function TagCheckboxItem({
    tag,
    count,
    total,
    checked,
    indeterminate,
    isPending,
    info,
    onChange,
    disabled
}: TagCheckboxItemProps) {
    const checkboxRef = useRef<HTMLInputElement>(null);
    const fmtDate = (iso: string) => new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    const fmtStamp = (iso: string) => new Date(iso).toLocaleString();
    const createdLine = info
        ? (info.created_at
            ? `created ${fmtDate(info.created_at)}${info.created_by ? ` by ${info.created_by}` : ''}`
            : 'created: unknown')
        : null;
    const createdTitle = info
        ? [
            info.created_at ? `Created ${fmtStamp(info.created_at)}${info.created_by ? ` by ${info.created_by}` : ''}` : 'Created: unknown (predates the tag registry, or added with the sample)',
            info.last_used_at ? `Last applied ${fmtStamp(info.last_used_at)}` : null,
        ].filter(Boolean).join('\n')
        : undefined;

    // Set indeterminate state (can't be done via JSX attribute)
    useEffect(() => {
        if (checkboxRef.current) {
            checkboxRef.current.indeterminate = indeterminate;
        }
    }, [indeterminate]);

    return (
        <label data-testid="tag-row" data-tag={tag} className={`
      flex items-center justify-between p-3 rounded-md border
      ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50'}
      ${checked && !indeterminate ? 'border-cyan-500 bg-cyan-50 dark:bg-cyan-900/20' : 'border-gray-200 dark:border-gray-700'}
      ${isPending ? 'ring-2 ring-yellow-400 dark:ring-yellow-600' : ''}
    `}>
            <div className="flex items-center gap-3 flex-1">
                <input
                    ref={checkboxRef}
                    type="checkbox"
                    checked={checked}
                    onChange={(e) => onChange(e.target.checked)}
                    disabled={disabled}
                    className="w-4 h-4 rounded border-gray-300 text-cyan-600 focus:ring-cyan-500 focus:ring-2"
                />
                <div className="min-w-0">
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                        {tag}
                        {isPending && <span className="ml-2 text-xs text-yellow-600 dark:text-yellow-400">●</span>}
                    </span>
                    {createdLine && (
                        <div data-testid="tag-created" title={createdTitle} className="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                            {createdLine}
                        </div>
                    )}
                </div>
            </div>

            <span className={`
        text-sm font-medium px-2 py-1 rounded
        ${indeterminate
                    ? 'text-yellow-700 bg-yellow-100 dark:text-yellow-300 dark:bg-yellow-900/30'
                    : checked
                        ? 'text-green-700 bg-green-100 dark:text-green-300 dark:bg-green-900/30'
                        : 'text-gray-500 bg-gray-100 dark:text-gray-400 dark:bg-gray-700'
                }
      `}>
                {isPending ? (checked ? total : 0) : count}/{total}
            </span>
        </label>
    );
}
