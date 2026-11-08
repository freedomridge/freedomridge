// Browser settings read from the frontend server's environment when it runs, not compiled in by
// `next build`, so one published image serves every deployment. MAPBOX_TOKEN is the map's Mapbox
// public token. NEXT_PUBLIC_MAPBOX_TOKEN, compiled in at build time (frontend/.env.local in
// development), is the fallback.
export const dynamic = 'force-dynamic';

export function GET() {
    return Response.json({ mapboxToken: process.env.MAPBOX_TOKEN || process.env.NEXT_PUBLIC_MAPBOX_TOKEN || null });
}
