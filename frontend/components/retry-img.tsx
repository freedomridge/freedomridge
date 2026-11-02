'use client';

import { useEffect, useRef, useState, type ImgHTMLAttributes } from 'react';

/** Backoff before each retry. After the last one the tile gives up and reports the failure. */
export const RETRY_DELAYS_MS = [800, 2000, 5000];

/**
 * An <img> that retries a failed load instead of staying blank.
 *
 * A grid tile's load fails transiently all the time: a lazy load the browser cancelled mid-scroll,
 * a VPN hiccup, a cold S3 fetch that took too long. A plain <img> then shows nothing until the tile
 * is unmounted and remounted — users discovered that "switching the view fixes it". This retries
 * up to RETRY_DELAYS_MS.length times; each retry carries a `retry=N` query param so the browser
 * issues a fresh request (the media endpoint ignores unknown params, so the server-side cache key
 * is unchanged). Only when the last retry has failed does the caller's onError run — a failure that
 * survives retries is the one worth a session check, and one event per tile instead of one per blip
 * keeps the /api/auth/me storm away.
 */
export function RetryImg({ src, alt = '', onError, ...rest }: Omit<ImgHTMLAttributes<HTMLImageElement>, 'src'> & { src: string }) {
    const [attempt, setAttempt] = useState(0);
    const [attemptSrc, setAttemptSrc] = useState(src);
    const [failed, setFailed] = useState(false);
    const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
    // A new source is a new image: start the attempt count over (state adjusted during render,
    // the React-recommended form of "reset state when a prop changes").
    if (src !== attemptSrc) {
        setAttemptSrc(src);
        setAttempt(0);
        setFailed(false);
    }
    // Cancel a pending retry when the source changes or the tile unmounts.
    useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, [src]);
    const effectiveSrc = attempt === 0 ? src : `${src}${src.includes('?') ? '&' : '?'}retry=${attempt}`;
    if (failed) {
        // Retries exhausted: say so. It was a blank square with the browser's 16 px broken-image
        // glyph, indistinguishable from a tile still loading.
        return (
            <div
                className={`${rest.className ?? ''} flex items-center justify-center bg-zinc-900 text-[11px] text-zinc-500`}
                style={rest.style}
                data-testid="media-unavailable"
                title="This sample's media could not be loaded"
            >
                Unavailable
            </div>
        );
    }
    return (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated media proxy, next/image not applicable
        <img
            {...rest}
            alt={alt}
            src={effectiveSrc}
            data-retry-attempt={attempt || undefined}
            onError={(e) => {
                if (attempt < RETRY_DELAYS_MS.length) {
                    if (timer.current) clearTimeout(timer.current);
                    timer.current = setTimeout(() => setAttempt(a => a + 1), RETRY_DELAYS_MS[attempt]);
                    return;
                }
                setFailed(true);
                onError?.(e);
            }}
        />
    );
}
