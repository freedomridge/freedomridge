'use client';

import { useEffect } from 'react';

// Catches errors thrown in the ROOT layout itself (which app/error.tsx cannot). Must render its own
// <html>/<body> because it replaces the root layout.
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
    useEffect(() => {
        console.error('Global error boundary caught:', error);
    }, [error]);

    return (
        <html lang="en" className="dark">
            <body className="bg-zinc-950 text-zinc-50">
                <div className="min-h-screen flex items-center justify-center p-6">
                    <div className="max-w-md text-center space-y-4">
                        <h2 className="text-lg font-semibold text-red-400">Application error</h2>
                        <p className="text-sm text-zinc-400">A critical error occurred. Reload the page to continue.</p>
                        <button
                            onClick={() => reset()}
                            className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-medium"
                        >
                            Reload
                        </button>
                    </div>
                </div>
            </body>
        </html>
    );
}
