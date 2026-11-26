// Named regions in the app (docs/REGIONS.md): the chip of a stage that is one region test, whatever made it (a `q=`
// link, a filter card's text, a saved view, an `f=` link, the assistant), and the region's outline on the map.
import type { MultiLineString, MultiPolygon, Position } from 'geojson';
import type { ComplexFilter, Region } from './api';
import { type RegionTest, regionTestOf } from './filter-query';

export { type RegionTest, regionTestOf };

/** A region test's filter (regionTestOf) turned around: included becomes excluded, and back. */
export function toggledRegionFilter(filter: ComplexFilter): ComplexFilter {
    const flip = (node: ComplexFilter): ComplexFilter => ({
        ...node,
        operator: node.operator === 'geo_region_exclude' ? 'geo_region' : 'geo_region_exclude',
    });
    return filter.logic ? { ...filter, filters: (filter.filters ?? []).map(flip) } : flip(filter);
}

/** The region tests among the stages' filters, in their order. */
export function regionTests(filters: (ComplexFilter | undefined)[]): RegionTest[] {
    return filters.map(regionTestOf).filter((test): test is RegionTest => test !== null);
}

/**
 * The chip's words: "In Kansas, United States of America", "Not in Kansas, United States of America", a country by its
 * name alone. Without the region (its name still loading, or a key this server has no region for) the key stands in
 * for the name: "In US-KZ".
 */
export function regionChipLabel(test: RegionTest, region?: Pick<Region, 'kind' | 'name' | 'country_name'> | null): string {
    const place = !region ? test.key
        : region.kind === 'country' || !region.country_name || region.country_name === region.name ? region.name
            : `${region.name}, ${region.country_name}`;
    return `${test.exclude ? 'Not in' : 'In'} ${place}`;
}

// Keys match in any case (docs/REGIONS.md): `us-ks` is `US-KS`, `us:Midwest` is `US:midwest`.
const sameKey = (key: string) => key.toUpperCase();

/**
 * The keys included in `now` that `before` didn't apply, included or excluded: the regions just applied, which the map
 * fits to as it does to a polygon just drawn. Turning a chip around, removing one, or keeping one applies none.
 */
export function newlyIncluded(before: RegionTest[], now: RegionTest[]): string[] {
    const had = new Set(before.map(test => sameKey(test.key)));
    return now.filter(test => !test.exclude && !had.has(sameKey(test.key))).map(test => test.key);
}

/** A region's box [west, south, east, north] as the corners of bounds for the map: across the antimeridian, east past 180. */
export function bboxCorners([west, south, east, north]: [number, number, number, number]): [[number, number], [number, number]] {
    return [[west, south], [east < west ? east + 360 : east, north]];
}

// An edge along the antimeridian: both ends on longitude 180, or both on -180.
const atAntimeridian = (point: Position) => Math.abs(Math.abs(point[0]) - 180) < 1e-6;
const onAntimeridian = (a: Position, b: Position) => atAntimeridian(a) && atAntimeridian(b) && Math.sign(a[0]) === Math.sign(b[0]);

/**
 * The lines of a region's outline. A region across the antimeridian (Chukotka, Fiji, the United States) comes in
 * pieces cut at it, whose rings run along longitude 180 and -180: drawn as they are, the cut would show as a border
 * down the middle of the region. The lines leave out every edge along the antimeridian.
 */
export function outlineLines(outline: MultiPolygon): MultiLineString {
    const lines: Position[][] = [];
    for (const polygon of outline.coordinates) {
        for (const ring of polygon) {
            let line: Position[] = [];
            ring.forEach((point, i) => {
                if (i > 0 && onAntimeridian(ring[i - 1], point)) {
                    if (line.length > 1) lines.push(line);
                    line = [];
                }
                line.push(point);
            });
            if (line.length > 1) lines.push(line);
        }
    }
    return { type: 'MultiLineString', coordinates: lines };
}
