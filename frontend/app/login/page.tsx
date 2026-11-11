'use client';

import React, { useState, useEffect, Suspense } from 'react';
import { useUser } from '@/contexts/UserContext';
import { useSearchParams } from 'next/navigation';
import Image from 'next/image';
import { Wordmark } from '@/components/wordmark';
import { safeNext } from '@/lib/links';

function LoginContent() {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const [demoMode, setDemoMode] = useState(false);
    const [isDemoLoading, setIsDemoLoading] = useState(false);
    const { login, checkAuth } = useUser();
    const searchParams = useSearchParams();
    const expired = searchParams ? searchParams.get('expired') : null;
    // The page asked for before signing in (a link, or the page a session expired on), if it is a page of this app.
    const next = () => safeNext(searchParams?.get('next'), window.location.origin);

    // Check if demo mode is enabled
    useEffect(() => {
        fetch('/api/config', { credentials: 'include' })
            .then(res => res.json())
            .then(data => setDemoMode(data.demo_mode))
            .catch(() => setDemoMode(false));
    }, []);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setIsLoading(true);
        try {
            await login(username, password, next());
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : String(err));
            setIsLoading(false);
        }
    };

    const handleDemoLogin = async () => {
        setError('');
        setIsDemoLoading(true);
        try {
            const res = await fetch('/api/auth/demo-login', {
                method: 'POST',
                credentials: 'include'
            });
            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to create demo account');
            }
            // Refresh auth state and go to the page asked for, else the homepage
            await checkAuth();
            // A full load: a new guest session must discard the previous user’s in-memory view state.
            window.location.href = next();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : String(err));
            setIsDemoLoading(false);
        }
    };

    return (
        <div className="min-h-screen w-full flex items-center justify-center bg-zinc-950 relative overflow-hidden">
            {/* Animated Background Effects */}
            <div className="absolute top-0 left-0 w-full h-full overflow-hidden z-0">
                <div className="absolute top-[-50%] left-[-50%] w-[200%] h-[200%] bg-[radial-gradient(circle_at_center,_var(--tw-gradient-stops))] from-cyan-900/10 via-zinc-950 to-zinc-950 animate-pulse"></div>
                <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[800px] h-[800px] bg-violet-900/10 rounded-full blur-3xl"></div>
            </div>

            <div className="relative z-10 w-full max-w-md p-8">
                <div className="bg-zinc-900/50 backdrop-blur-xl border border-zinc-800 rounded-2xl shadow-2xl p-8 space-y-8">
                    <div className="group flex flex-col items-center text-center space-y-4">
                        <div className="relative w-20 h-20 rounded-2xl overflow-hidden shadow-lg shadow-cyan-500/20 ring-1 ring-white/10">
                            <Image src="/logo.png" alt="Freedom Ridge" fill priority sizes="80px" className="object-cover" />
                        </div>
                        <div>
                            <h1 className="text-3xl font-bold">
                                <Wordmark className="justify-items-center" />
                            </h1>
                            <p className="text-zinc-400 text-sm mt-1">Open the door. See every sample.</p>
                        </div>
                    </div>

                    {expired && (
                        <div className="text-yellow-400 text-sm text-center bg-yellow-900/20 p-3 rounded border border-yellow-900/50">
                            Your session has expired. Please sign in again.
                        </div>
                    )}

                    <form className="space-y-6" onSubmit={handleSubmit}>
                        <div className="space-y-4">
                            <div>
                                <label htmlFor="username" className="block text-sm font-medium text-zinc-300 mb-1">
                                    Username
                                </label>
                                <input
                                    id="username"
                                    name="username"
                                    type="text"
                                    required
                                    className="appearance-none block w-full px-3 py-2 border border-zinc-700 rounded-lg placeholder-zinc-500 text-white bg-zinc-950/50 focus:outline-none focus:ring-2 focus:ring-cyan-500 focus:border-transparent transition-all sm:text-sm"
                                    placeholder="Enter your username"
                                    value={username}
                                    onChange={(e) => setUsername(e.target.value)}
                                />
                            </div>
                            <div>
                                <label htmlFor="password" className="block text-sm font-medium text-zinc-300 mb-1">
                                    Password
                                </label>
                                <input
                                    id="password"
                                    name="password"
                                    type="password"
                                    required
                                    className="appearance-none block w-full px-3 py-2 border border-zinc-700 rounded-lg placeholder-zinc-500 text-white bg-zinc-950/50 focus:outline-none focus:ring-2 focus:ring-cyan-500 focus:border-transparent transition-all sm:text-sm"
                                    placeholder="Enter your password"
                                    value={password}
                                    onChange={(e) => setPassword(e.target.value)}
                                />
                            </div>
                        </div>

                        {error && (
                            <div className="text-red-400 text-sm text-center bg-red-900/20 p-2 rounded border border-red-900/50">
                                {error}
                            </div>
                        )}

                        <button
                            type="submit"
                            disabled={isLoading || isDemoLoading}
                            className="w-full flex justify-center py-2.5 px-4 border border-transparent rounded-lg shadow-sm text-sm font-medium text-black bg-cyan-500 hover:bg-cyan-400 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-cyan-500 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                        >
                            {isLoading ? 'Signing in...' : 'Sign in'}
                        </button>
                    </form>

                    {/* Demo Mode: Try as Guest */}
                    {demoMode && (
                        <div className="mt-4">
                            <div className="relative">
                                <div className="absolute inset-0 flex items-center">
                                    <div className="w-full border-t border-zinc-700"></div>
                                </div>
                                <div className="relative flex justify-center text-sm">
                                    <span className="px-2 bg-zinc-900/50 text-zinc-500">or</span>
                                </div>
                            </div>
                            <button
                                type="button"
                                onClick={handleDemoLogin}
                                disabled={isLoading || isDemoLoading}
                                className="mt-4 w-full flex justify-center py-2.5 px-4 border border-zinc-600 rounded-lg shadow-sm text-sm font-medium text-zinc-300 bg-zinc-800/50 hover:bg-zinc-700/50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-zinc-500 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                            >
                                {isDemoLoading ? 'Creating demo account...' : '✨ Try as Guest'}
                            </button>
                            <p className="mt-2 text-xs text-center text-zinc-500">
                                No account needed. Explore with limited access.
                            </p>
                        </div>
                    )}
                </div>

                <p className="mt-8 text-center text-xs text-zinc-600">
                    &copy; {new Date().getFullYear()} Freedom Ridge contributors · MIT License
                </p>
            </div>
        </div>
    );
}

export default function LoginPage() {
    return (
        <Suspense fallback={<div className="min-h-screen w-full bg-zinc-950" />}>
            <LoginContent />
        </Suspense>
    );
}

