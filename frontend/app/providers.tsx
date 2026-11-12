'use client';

import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { useState, useEffect } from 'react';
import { UserProvider } from '@/contexts/UserContext';
import { ToastProvider, useToast } from '@/contexts/ToastContext';
import { isRescoringRefusal, rescoringRetryPeriod } from '@/lib/api';
import { coalesceQueryNotifications } from '@/lib/query-notify';
import { loginPath } from '@/lib/links';

// Query results that arrive together render together (the sidebar's streamed fields).
coalesceQueryNotifications();

function is401(error: unknown): boolean {
    return (error as { status?: number } | null | undefined)?.status === 401;
}

type StatusError = { status?: number; message?: string } | null | undefined;

// Whether a failure repeats the dataset's own refusal (403) or absence (404): the dataset page answers
// that with one clear message, so the same error from its panels' queries is not news.
function repeatsDatasetError(queryClient: QueryClient, error: unknown): boolean {
    const { status, message } = (error ?? {}) as NonNullable<StatusError>;
    if (status !== 403 && status !== 404) return false;
    return queryClient.getQueryCache().findAll({ queryKey: ['dataset'] }).some(query => {
        const own = query.state.error as StatusError;
        return query.state.status === 'error' && own?.status === status && own?.message === message;
    });
}

function datasetLoading(queryClient: QueryClient): boolean {
    return queryClient.getQueryCache().findAll({ queryKey: ['dataset'] }).some(query => query.state.fetchStatus === 'fetching');
}

// Default error surface for ALL query failures: without this, a non-401 failure (500/timeout/network)
// renders identically to genuine empty data or a perpetual spinner in each panel. 401 is handled
// separately (redirect to login). Deduped per query+error so background refetch loops don't spam.
function QueryErrorToaster() {
    const queryClient = useQueryClient();
    const { addToast } = useToast();
    useEffect(() => {
        const seen = new Map<string, number>();
        // The panels' queries start with the dataset's own and usually fail first, so a 403/404 waits
        // while the dataset query loads: dropped if the dataset failed the same way, toasted otherwise.
        let held: unknown[] = [];
        const toast = (err: unknown) => {
            const msg = (err as { message?: string } | null)?.message || 'Request failed';
            addToast(`Couldn't load data: ${msg}`, 'error', 5000);
        };
        return queryClient.getQueryCache().subscribe((event) => {
            if (event.type !== 'updated') return;
            if (held.length && event.query.queryKey[0] === 'dataset' && event.query.state.fetchStatus === 'idle') {
                const waiting = held;
                held = [];
                waiting.filter(err => !repeatsDatasetError(queryClient, err)).forEach(toast);
            }
            if (event.query.state.status !== 'error') return;
            const err = event.query.state.error;
            if (is401(err)) return;
            if (event.query.meta?.errorHandledLocally === true) return;
            const at = event.query.state.errorUpdatedAt;
            if (seen.get(event.query.queryHash) === at) return; // already toasted this occurrence
            seen.set(event.query.queryHash, at);
            if (repeatsDatasetError(queryClient, err)) return;
            const status = (err as StatusError)?.status;
            if ((status === 403 || status === 404) && datasetLoading(queryClient)) {
                held.push(err);
                return;
            }
            toast(err);
        });
    }, [queryClient, addToast]);
    return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
    const [queryClient] = useState(() => new QueryClient({
        defaultOptions: {
            queries: {
                staleTime: 60 * 1000,
                retry: (failureCount, error: unknown) => {
                    // A concept being rescored refuses filters until its job ends: keep retrying,
                    // so the view recovers by itself (and never sits on a spinner for good).
                    if (isRescoringRefusal(error)) return true;
                    const s = (error as { status?: number } | null | undefined)?.status;
                    // Rate limits are transient, like 5xx/network failures.
                    if (s === 429) return failureCount < 3;
                    // Other 4xx are deterministic client errors — retrying just delays the
                    // inevitable failure and burns up to ~13s on exponential backoff.
                    if (s && s >= 400 && s < 500) return false;
                    return failureCount < 3;
                },
                retryDelay: (attempt, error: unknown) =>
                    isRescoringRefusal(error) ? rescoringRetryPeriod(attempt) : Math.min(1000 * 2 ** attempt, 10000),
            },
            mutations: {
                retry: false,
            },
        },
    }));

    // Redirect to login on ANY 401 — queries AND mutations. Without this, an
    // expired session causes queries to silently fail while the user stares at
    // stale cached data with no indication their session died.
    useEffect(() => {
        let redirecting = false;
        function handleSessionExpired() {
            // Avoid loops: don't redirect if we're already on (or heading to) login.
            if (redirecting || window.location.pathname === '/login') return;
            redirecting = true;
            queryClient.clear();
            // A full load: an expired session must unmount every authenticated panel and clear its local state.
            // Signing in again comes back to this page.
            window.location.href = loginPath(window.location, true);
        }

        const unsubQuery = queryClient.getQueryCache().subscribe((event) => {
            if (event.type === 'updated' && event.query.state.status === 'error') {
                if (is401(event.query.state.error)) handleSessionExpired();
            }
        });
        const unsubMutation = queryClient.getMutationCache().subscribe((event) => {
            if (event.type === 'updated' && event.mutation?.state.status === 'error') {
                if (is401(event.mutation.state.error)) handleSessionExpired();
            }
        });
        // Also fire for non-react-query 401s: raw fetches and swallowed errors
        // dispatch `fridge:unauthorized` (see ApiError), and the session watchdog
        // dispatches it when /api/auth/me starts returning 401 (e.g. images 401ing
        // but no data request is in flight).
        window.addEventListener('fridge:unauthorized', handleSessionExpired);

        return () => {
            unsubQuery();
            unsubMutation();
            window.removeEventListener('fridge:unauthorized', handleSessionExpired);
        };
    }, [queryClient]);

    return (
        <QueryClientProvider client={queryClient}>
            <UserProvider>
                <ToastProvider>
                    <QueryErrorToaster />
                    {children}
                </ToastProvider>
            </UserProvider>
        </QueryClientProvider>
    );
}
