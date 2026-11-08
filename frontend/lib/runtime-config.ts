import { useQuery } from '@tanstack/react-query';

export type RuntimeConfig = { mapboxToken: string | null };

// The frontend server's runtime settings (app/runtime-config/route.ts), read once per page load.
export async function fetchRuntimeConfig(): Promise<RuntimeConfig> {
    const res = await fetch('/runtime-config');
    if (!res.ok) throw new Error(`Failed to load the app's settings: ${res.status} ${res.statusText}`);
    return res.json();
}

export function useRuntimeConfig() {
    return useQuery({ queryKey: ['runtime-config'], queryFn: fetchRuntimeConfig, staleTime: Infinity, gcTime: Infinity });
}
