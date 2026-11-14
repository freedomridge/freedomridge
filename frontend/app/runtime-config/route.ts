// Browser settings read from the frontend server's environment when it runs, not compiled in by
// `next build`, so one published image serves every deployment. MAPBOX_TOKEN is the map's Mapbox
// public token. NEXT_PUBLIC_MAPBOX_TOKEN, compiled in at build time (frontend/.env.local in
// development), is the fallback. FRIDGE_LIVE_ADDRESS=off (or false, 0, no) keeps the filters, sort
// and open sample out of the dataset and snapshot pages' address; on when unset.
export const dynamic = 'force-dynamic';

export function GET() {
    return Response.json({
        mapboxToken: process.env.MAPBOX_TOKEN || process.env.NEXT_PUBLIC_MAPBOX_TOKEN || null,
        liveAddress: !['off', 'false', '0', 'no'].includes((process.env.FRIDGE_LIVE_ADDRESS ?? '').trim().toLowerCase()),
    });
}
