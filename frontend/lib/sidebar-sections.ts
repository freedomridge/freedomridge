import type { ComplexFilter, Filters } from './api';
import { convertSimpleFiltersToComplex } from './filter-utils';

// The filter sidebar's collapsible field sections: which
// fields filter the view, and which ones the user expanded or collapsed on a dataset.

/** Names of the fields the sidebar's draft filters or the applied view stages constrain. */
export function filteredFieldNames(drafts: Filters, applied?: ComplexFilter): Set<string> {
    const names = new Set<string>();
    const visit = (node: ComplexFilter | undefined) => {
        if (!node) return;
        if (typeof node.field === 'string') names.add(node.field);
        node.filters?.forEach(visit);
    };
    // The drafts' keys go through the same conversion Apply uses (exclude_ prefixes, _min/_max bounds).
    visit(convertSimpleFiltersToComplex(drafts));
    visit(applied);
    return names;
}

const storageKey = (datasetId: number, state: 'expanded' | 'collapsed') => `fridge-sidebar-${state}-${datasetId}`;

function readFields(datasetId: number, state: 'expanded' | 'collapsed'): string[] {
    try {
        const parsed: unknown = JSON.parse(window.localStorage.getItem(storageKey(datasetId, state)) || '[]');
        return Array.isArray(parsed) ? parsed.filter((name): name is string => typeof name === 'string') : [];
    } catch {
        return [];
    }
}

/** The fields the user expanded on this dataset, as remembered by this browser. */
export function readExpandedFields(datasetId: number): string[] {
    return readFields(datasetId, 'expanded');
}

/** The fields the user collapsed on this dataset, as remembered by this browser. */
export function readCollapsedFields(datasetId: number): string[] {
    return readFields(datasetId, 'collapsed');
}

/**
 * Remember that the user expanded or collapsed one field: it joins one list and leaves the other. The
 * stored lists are read again first, so what another tab or panel stored meanwhile survives, and names
 * the dataset no longer has (`knownNames`; none known yet = keep all) are dropped.
 */
export function storeFieldExpanded(datasetId: number, name: string, expanded: boolean, knownNames: string[]): void {
    const known = new Set(knownNames);
    const others = (state: 'expanded' | 'collapsed') =>
        readFields(datasetId, state).filter(other => other !== name && (known.size === 0 || known.has(other)));
    const [joined, left] = expanded ? ['expanded', 'collapsed'] as const : ['collapsed', 'expanded'] as const;
    try {
        window.localStorage.setItem(storageKey(datasetId, joined), JSON.stringify([...others(joined), name]));
        window.localStorage.setItem(storageKey(datasetId, left), JSON.stringify(others(left)));
    } catch {
        // No storage (private window, blocked site data): the sections stay as they are for this page.
    }
}
