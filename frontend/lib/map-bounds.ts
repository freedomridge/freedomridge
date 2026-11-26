// The region the map asks the server for (docs/MAP_REQUIREMENTS.md #13), and the boxes it fits to (#7, #12), used by
// components/map-panel.tsx.

// Mirror of backend _MAP_POINT_ZOOM (docs/MAP_REQUIREMENTS.md #1): at/above this zoom
// the server returns individual points, so we DON'T enlarge the fetch region there
// (it would risk the point cap).
export const MAP_POINT_ZOOM = 12;

export type Bbox = [number, number, number, number];

// [[west, south], [east, north]], as mapbox-gl's fitBounds takes them. An east past 180 is a box across the
// antimeridian.
export type Corners = [[number, number], [number, number]];

// The corners to fit for a map_extent answer (docs/MAP_REQUIREMENTS.md #7). Across the antimeridian the server answers
// west greater than east, as GeoJSON does: the east goes 360 degrees on, past 180, so the box is the narrow one across
// it and not the rest of the world. A box with no width or height (samples at one spot) is widened a little so the fit
// doesn't slam to its maxZoom.
export function extentCorners({ w, s, e, n }: { w: number; s: number; e: number; n: number }): Corners {
    if (e < w) e += 360;
    if (e - w < 1e-6) { w -= 0.0005; e += 0.0005; }
    if (n - s < 1e-6) { s -= 0.0005; n += 0.0005; }
    return [[w, s], [e, n]];
}

// The corners of the box that holds these points (the selected samples, #12), as map_extent answers. When the points
// fit in half the globe across the antimeridian (between two of them, in longitude order, is a stretch with no point
// at least 180 degrees wide), the box leaves that stretch out and crosses the antimeridian, its east past 180.
// Otherwise it is their smallest and largest longitude, as it always was.
export function pointsCorners(points: ReadonlyArray<{ lon: number; lat: number }>): Corners {
    const lons = points.map(p => p.lon).sort((a, b) => a - b);
    const lats = points.map(p => p.lat);
    let west = lons[0];
    let east = lons[lons.length - 1];
    for (let i = 1; i < lons.length; i++) {
        if (lons[i] - lons[i - 1] >= 180) { west = lons[i]; east = lons[i - 1] + 360; }
    }
    return [[west, Math.min(...lats)], [east, Math.max(...lats)]];
}

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
