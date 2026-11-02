'use client';

import { useSyncExternalStore } from 'react';
import { hashKey, useQuery, type QueryClient } from '@tanstack/react-query';
import { getSamples, samplesMutationVersion, SAMPLES_MUTATED_EVENT, type ComplexFilter } from '@/lib/api';

// The first element of every count's query key. A "refresh this dataset's views" invalidation that
// follows notifySamplesMutated leaves these out: the version it moved already asks each count again,
// and refetching the old version's key in the same tick sent every count twice.
export const SAMPLE_COUNT_KEY = 'samples-count';

// The dataset stays second: "refresh this dataset's views" invalidates by queryKey[1].
function sampleCountKey(datasetId: number | null | undefined, filters: ComplexFilter, version: number) {
    return [SAMPLE_COUNT_KEY, datasetId, filters, version] as const;
}

function subscribe(onChange: () => void) {
    window.addEventListener(SAMPLES_MUTATED_EVENT, onChange);
    return () => window.removeEventListener(SAMPLES_MUTATED_EVENT, onChange);
}

/**
 * How many samples of the dataset match `filters`, keyed by exactly what is asked: one COUNT request
 * (limit 0) per dataset, filter tree and mutation version, whoever asks. The top bar (applied filters,
 * image grain) and a grid (its staged filters and grain) ask the same thing until a draft, a selection
 * or the grain sets them apart; separate keys would count the dataset twice when it opens.
 * notifySamplesMutated moves the version, so every count of the dataset is asked
 * again. `enabled` false holds the request back, e.g. while the filter's shape is not known yet.
 * `keepWhileRecounting` shows the previous number while the same filters are counted again after a
 * change (never across a filter change, where the old number would be wrong).
 */
export function useSampleCount(
    datasetId: number | null | undefined,
    filters: ComplexFilter,
    { enabled = true, keepWhileRecounting = false }: { enabled?: boolean; keepWhileRecounting?: boolean } = {},
) {
    const version = useSyncExternalStore(subscribe, () => (datasetId ? samplesMutationVersion(datasetId) : 0), () => 0);
    const queryKey = sampleCountKey(datasetId, filters, version);
    return useQuery({
        queryKey,
        queryFn: async () => (await getSamples(datasetId!, 0, 0, filters)).total,
        enabled: !!datasetId && enabled,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        placeholderData: keepWhileRecounting
            ? (previous, previousQuery) =>
                previousQuery && hashKey(previousQuery.queryKey.slice(0, 3)) === hashKey(queryKey.slice(0, 3)) ? previous : undefined
            : undefined,
    });
}

/**
 * Ask the count again now, from scratch. An invalidation does not do that while the count waits out a
 * retry (a rescoring refusal, a 5xx backoff) and has no number yet: it waits for its next turn.
 */
export function resetSampleCount(queryClient: QueryClient, datasetId: number, filters: ComplexFilter) {
    return queryClient.resetQueries({ queryKey: sampleCountKey(datasetId, filters, samplesMutationVersion(datasetId)), exact: true });
}
