// The region the map asks the server for (docs/MAP_REQUIREMENTS.md #13), used by components/map-panel.tsx.

// Mirror of backend _MAP_POINT_ZOOM (docs/MAP_REQUIREMENTS.md #1): at/above this zoom
// the server returns individual points, so we DON'T enlarge the fetch region there
// (it would risk the point cap).
export const MAP_POINT_ZOOM = 12;

export type Bbox = [number, number, number, number];

// mapbox-gl's getBounds() longitudes are unwrapped: a view across the antimeridian is west 170, east 190, the same
// view on a repeated world copy 530 to 550, and a view wider than the world spans 360 degrees or more. Shift the west
// into [-180, 180) and keep the span, so every world copy asks for the same region. An east past 180 is a view across
// the antimeridian, which the server answers for both sides (_map_lon_window in backend/routers/datasets.py).
export function viewBbox([w, s, e, n]: Bbox): Bbox {
    if (e - w >= 360) return [-180, s, 180, n];
    const shift = Math.floor((w + 180) / 360) * 360;
    return [w - shift, s, e - shift, n];
}

// Tile the viewport: snap the fetched bbox out to a fixed per-zoom world grid so that
// panning WITHIN a tile produces the same request and hits the react-query cache,
// instead of refetching on every pixel of pan. The grid depends only on zoom, so the
// same tiles recur as you pan (and the whole-world view snaps to one stable tile).
// Returns the viewport unchanged at points zooms. See docs/MAP_REQUIREMENTS.md #13.
// Takes a viewBbox: a tile may end past 180 (across the antimeridian); one 360 degrees wide is the whole world.
export function tileBbox(bbox: Bbox, zoom: number): Bbox {
    if (zoom >= MAP_POINT_ZOOM) return bbox;
    const step = 360 / Math.pow(2, Math.max(0, zoom - 1)); // ~2 web-mercator tiles wide
    const [w, s, e, n] = bbox;
    const west = Math.floor(w / step) * step;
    const east = Math.ceil(e / step) * step;
    const world = east - west >= 360;
    return [
        world ? -180 : west,
        Math.max(-85, Math.floor(s / step) * step),
        world ? 180 : east,
        Math.min(85, Math.ceil(n / step) * step),
    ];
}
