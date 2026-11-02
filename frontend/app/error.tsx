'use client';

import { useEffect } from 'react';

// Route-level error boundary. Without this, any render-phase throw (e.g. a malformed sample in a
// panel renderer) unmounts the whole app to a blank page in a production build. This catches it and
// offers recovery instead of a white screen.
export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
    useEffect(() => {
        console.error('Route error boundary caught:', error);
    }, [error]);

    return (
        <div className="min-h-screen flex items-center justify-center bg-zinc-950 text-zinc-50 p-6">
            <div className="max-w-md text-center space-y-4">
                <h2 className="text-lg font-semibold text-red-400">Something went wrong</h2>
                <p className="text-sm text-zinc-400">
                    An unexpected error occurred while rendering this page. Your data is safe — try again, or reload.
                </p>
                <button
                    onClick={() => reset()}
                    className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-medium"
                >
                    Try again
                </button>
            </div>
        </div>
    );
}
