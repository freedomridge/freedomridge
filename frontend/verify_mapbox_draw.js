try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- standalone Node verification script probing the module's CommonJS export shape; a static import can't be try/caught
    const MapboxDraw = require('@mapbox/mapbox-gl-draw');
    console.log('Type of export:', typeof MapboxDraw);
    console.log('Is class/function?', typeof MapboxDraw === 'function');
    console.log('Has default?', 'default' in MapboxDraw);
    if (MapboxDraw.default) {
        console.log('Type of default:', typeof MapboxDraw.default);
    }
} catch (e) {
    console.error('Error requiring:', e);
}
