'use client';

import React, { createContext, useContext, useState, useEffect, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useQueryClient } from '@tanstack/react-query';

interface User {
    id: number;
    username: string;
    email?: string;
    role: 'admin' | 'power_user' | 'guest';
    must_change_password: boolean;
    is_active: boolean;
    is_demo_account?: boolean;
    can_share_views?: boolean;
}

interface UserContextType {
    user: User | null;
    isLoading: boolean;
    login: (username: string, password: string) => Promise<void>;
    logout: () => Promise<void>;
    changePassword: (current: string, newPass: string) => Promise<void>;
    checkAuth: () => Promise<void>;
    isAuthenticated: boolean;
}

const UserContext = createContext<UserContextType | undefined>(undefined);

export function UserProvider({ children }: { children: React.ReactNode }) {
    const [user, setUser] = useState<User | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const isAuthenticated = !!user;
    const router = useRouter();
    const queryClient = useQueryClient();
    const authRetry = useRef<ReturnType<typeof setTimeout> | null>(null);
    const authRequest = useRef<Promise<void> | null>(null);
    const authController = useRef<AbortController | null>(null);

    // Named so the retry timer can call it: the hook's own `checkAuth` isn't declared yet here.
    const checkAuth = useCallback(function check(): Promise<void> {
        if (authRequest.current) return authRequest.current;
        if (authRetry.current !== null) clearTimeout(authRetry.current);
        authRetry.current = null;
        const controller = new AbortController();
        authController.current = controller;
        const retry = () => {
            if (!controller.signal.aborted) {
                authRetry.current = setTimeout(() => { void check(); }, 5000);
            }
        };
        authRequest.current = (async () => {
            try {
                const res = await fetch('/api/auth/me', { signal: controller.signal });
                if (res.ok) {
                    const userData = await res.json();
                    if (controller.signal.aborted) return;
                    // Keep the same object when nothing changed: the session watchdog runs this on
                    // a timer, and a new identity re-renders every user consumer (the grid then
                    // reloads its thumbnails).
                    setUser(previous => (previous && JSON.stringify(previous) === JSON.stringify(userData) ? previous : userData));
                    setIsLoading(false);
                } else if (res.status === 401) {
                    if (controller.signal.aborted) return;
                    setUser(null);
                    setIsLoading(false);
                    window.dispatchEvent(new Event('fridge:unauthorized'));
                } else {
                    // Busy/unreachable is not logged out. Keep the current user;
                    // on first load keep AuthGuard waiting until identity is known.
                    retry();
                }
            } catch {
                retry();
            } finally {
                if (authController.current === controller) authRequest.current = null;
            }
        })();
        return authRequest.current;
    }, []);

    // Check auth status on mount
    useEffect(() => {
        void checkAuth();
        return () => {
            authController.current?.abort();
            authRequest.current = null;
            if (authRetry.current !== null) clearTimeout(authRetry.current);
        };
    }, [checkAuth]);

    // Session watchdog: once logged in, detect an expired session promptly so the
    // app logs out instead of silently failing. This covers the case where only
    // media `<img>` requests are 401ing (native image loads bypass react-query and
    // ApiError, so nothing else would notice). Checks on tab focus, on a slow
    // interval, and on demand via the `fridge:check-session` event (which the grid
    // fires from an <img> onError). Debounced so a burst of failures = one check.
    useEffect(() => {
        if (!isAuthenticated) return;
        let inFlight = false;
        let lastCheck = 0;
        const verify = async () => {
            const now = Date.now();
            // Throttle to once per 30s: the `fridge:check-session` trigger fires on every grid
            // image onError, so a broken/aborted-image burst (or a slow scroll) must not turn into
            // a /api/auth/me storm. 30s still detects an expired session promptly (and tab-focus +
            // the 60s interval back it up) without the noise.
            if (inFlight || now - lastCheck < 30000) return;
            inFlight = true;
            lastCheck = now;
            try {
                // Share the 401-only decision and quiet retry with the initial check.
                await checkAuth();
            } finally {
                inFlight = false;
            }
        };
        const onVisible = () => { if (document.visibilityState === 'visible') verify(); };
        const interval = setInterval(verify, 60000);
        document.addEventListener('visibilitychange', onVisible);
        window.addEventListener('fridge:check-session', verify);
        return () => {
            clearInterval(interval);
            document.removeEventListener('visibilitychange', onVisible);
            window.removeEventListener('fridge:check-session', verify);
        };
    }, [isAuthenticated, checkAuth]);

    async function login(username: string, password: string) {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });

        if (!res.ok) {
            const error = await res.json();
            throw new Error(error.detail || 'Login failed');
        }

        await checkAuth();
        // Auth identity changed — drop any cache from the logged-out/previous session so
        // queries (e.g. ['datasets']) refetch authenticated instead of showing the stale
        // empty result that survived the client-side navigation. (A full refresh "worked"
        // only because it reset the cache; this makes client-side login behave the same.)
        queryClient.clear();
        router.push('/');
    }

    async function logout() {
        try {
            await fetch('/api/auth/logout', { method: 'POST' });
            authController.current?.abort();
            authRequest.current = null;
            if (authRetry.current !== null) clearTimeout(authRetry.current);
            setUser(null);
            queryClient.clear();  // don't leak the previous user's cached data into the next session
            router.push('/login');
        } catch (err) {
            console.error('Logout failed:', err);
        }
    }

    async function changePassword(current: string, newPass: string) {
        const wasRequired = !!user?.must_change_password;
        const res = await fetch('/api/auth/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                current_password: current,
                new_password: newPass
            }),
        });

        if (!res.ok) {
            const error = await res.json();
            throw new Error(error.detail || 'Password change failed');
        }

        // Optimistically clear the must_change_password flag so the modal can close
        setUser((prev) => prev ? { ...prev, must_change_password: false } : prev);

        // Refresh user to clear must_change_password flag
        await checkAuth();
        // While the change was required every API but auth answered 403, so the open page's queries
        // (the home page's dataset list) failed. Refetch them now that they can succeed.
        if (wasRequired) void queryClient.resetQueries();
    }

    return (
        <UserContext.Provider value={{
            user,
            isLoading,
            login,
            logout,
            changePassword,
            checkAuth,
            isAuthenticated
        }}>
            {children}
        </UserContext.Provider>
    );
}

export function useUser() {
    const context = useContext(UserContext);
    if (context === undefined) {
        throw new Error('useUser must be used within a UserProvider');
    }
    return context;
}
