/**
 * Image URL helpers.
 *
 * The canonical way to render a sample's image is via the sample-media proxy:
 *
 *   <img src={getSampleMediaUrl(sample.id)} />                 // primary filepath
 *   <img src={getSampleMediaUrl(sample.id, 'thumbnail_path')} /> // custom field
 *
 * The backend resolves dataset → storage_config → bucket/credentials and
 * applies access control. The raw `filepath` field on a sample stays as the
 * original path (s3://bucket/key or /local/abs/path) for display in the
 * metadata panel.
 *
 * For non-`filepath` fields the backend reads `metadata->>field`, so the value
 * stored there must be an s3:// URL, an absolute local path, or an http(s):// URL
 * (which the backend transparently 302-redirects to).
 *
 * Pass `overlay` to get back a server-composited image with the named heatmap
 * painted on top (used by the grid view so we don't need a Canvas per cell).
 *
 * Pass `transform` to get back a cropped and/or width-resized JPEG:
 *   - `crop: 'meta'` crops to the sample's `metadata.crop_bbox` (object/patch rows)
 *   - `crop: 'x,y,w,h'` crops an explicit box
 *   - `w: 256` resizes the output to that width (a cached thumbnail)
 */

export function getSampleMediaUrl(
    sampleId: number | string,
    field?: string,
    overlay?: string,
    overlayOpts?: { colormap?: string; alpha?: number; range?: [number, number] },
    transform?: { crop?: string; w?: number },
    versionId?: number,
): string {
    const base = `/api/samples/${sampleId}/media`;
    const params = new URLSearchParams();
    if (versionId != null) params.set('version_id', String(versionId));
    if (field && field !== 'filepath') params.set('field', field);
    if (transform?.crop) params.set('crop', transform.crop);
    if (transform?.w != null) params.set('w', String(transform.w));
    if (overlay) {
        params.set('overlay', overlay);
        // Per-field overlay config; passing it also busts the browser + server composite
        // cache when it changes (the composite cache key bakes colormap/alpha/range).
        if (overlayOpts?.colormap) params.set('colormap', overlayOpts.colormap);
        if (overlayOpts?.alpha != null) params.set('alpha', String(overlayOpts.alpha));
        if (overlayOpts?.range && overlayOpts.range.length === 2) {
            params.set('range_start', String(overlayOpts.range[0]));
            params.set('range_end', String(overlayOpts.range[1]));
        }
    }
    const qs = params.toString();
    return qs ? `${base}?${qs}` : base;
}
