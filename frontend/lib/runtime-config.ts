import { useState } from 'react';
import { queryOptions, useQuery } from '@tanstack/react-query';

export type RuntimeConfig = { mapboxToken: string | null; liveAddress: boolean };

// The frontend server's runtime settings (app/runtime-config/route.ts), read once per page load.
export async function fetchRuntimeConfig(): Promise<RuntimeConfig> {
    const res = await fetch('/runtime-config');
    if (!res.ok) throw new Error(`Failed to load the app's settings: ${res.status} ${res.statusText}`);
    return res.json();
}

export const runtimeConfigQuery = queryOptions({ queryKey: ['runtime-config'], queryFn: fetchRuntimeConfig, staleTime: Infinity, gcTime: Infinity });

export function useRuntimeConfig() {
    return useQuery(runtimeConfigQuery);
}

/**
 * Whether the dataset and snapshot pages keep what they show in the address (FRIDGE_LIVE_ADDRESS); undefined until
 * the server answers, then fixed while this page stays mounted (the next page opened in the tab reads it again). It
 * keeps filter text out of the address, so a request that fails counts as off, and a retry answering later changes
 * nothing for this page.
 */
export function useLiveAddress(): boolean | undefined {
    const { data, failureCount } = useRuntimeConfig();
    const answer = data ? data.liveAddress === true : failureCount > 0 ? false : undefined;
    const [decided, setDecided] = useState(answer);
    if (decided === undefined && answer !== undefined) setDecided(answer);
    return decided ?? answer;
}
