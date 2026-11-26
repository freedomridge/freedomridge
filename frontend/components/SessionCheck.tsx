'use client';

import { useEffect, useRef } from 'react';
import { usePathname } from 'next/navigation';
import { isCancelledError, useQueryClient } from '@tanstack/react-query';
import { useUser } from '@/contexts/UserContext';

// Whether a failed request carries no status the app can read: a page's own fetch that throws its own Error (an API
// call's error, ApiError in lib/api.ts, carries one and announces a session's end itself).
function withoutStatus(error: unknown): boolean {
    if (isCancelledError(error) || (error as { name?: unknown } | null)?.name === 'AbortError') return false;
    return (error as { status?: unknown } | null)?.status === undefined;
}

// A tab whose session ended elsewhere (signed out in another tab, expired, the account deactivated or deleted) asks the
// server who is signed in at each navigation inside the app, and so goes to sign in then instead of at the next
// periodic check (a page may ask nothing the server refuses: the datasets list answers a signed-out tab with none). A
// request of the page's own that fails without a status asks the session watchdog (UserContext) for a check, which it
// makes at most once in 30 seconds whatever asks: a page polling against a failing server, or every query giving up
// while the network is down, doesn't multiply the asking. A person still signed in stays.
export function SessionCheck() {
    const pathname = usePathname();
    const { isAuthenticated, checkAuth } = useUser();
    const queryClient = useQueryClient();
    const shown = useRef(pathname);

    useEffect(() => {
        if (shown.current === pathname) return;  // the page the tab opened on: UserContext asks as it starts
        shown.current = pathname;
        if (isAuthenticated) void checkAuth();
    }, [pathname, isAuthenticated, checkAuth]);

    useEffect(() => {
        if (!isAuthenticated) return;
        const failed = (error: unknown) => {
            if (withoutStatus(error)) window.dispatchEvent(new Event('fridge:check-session'));
        };
        const unsubscribeQueries = queryClient.getQueryCache().subscribe(event => {
            if (event.type === 'updated' && event.action.type === 'error') failed(event.action.error);
        });
        const unsubscribeMutations = queryClient.getMutationCache().subscribe(event => {
            if (event.type === 'updated' && event.action.type === 'error') failed(event.action.error);
        });
        return () => {
            unsubscribeQueries();
            unsubscribeMutations();
        };
    }, [isAuthenticated, queryClient]);

    return null;
}
