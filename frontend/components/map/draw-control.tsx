import { useControl, useMap } from 'react-map-gl/mapbox';
import type { ControlPosition } from 'react-map-gl/mapbox';
import { useEffect } from 'react';
import type MapboxDraw from '@mapbox/mapbox-gl-draw';

type DrawControlProps = {
    position?: ControlPosition;
    onCreate?: (evt: MapboxDraw.DrawCreateEvent) => void;
    onUpdate?: (evt: MapboxDraw.DrawUpdateEvent) => void;
    onDelete?: (evt: MapboxDraw.DrawDeleteEvent) => void;
    onModeChange?: (evt: MapboxDraw.DrawModeChangeEvent) => void;
    onDrawReady?: (draw: MapboxDraw) => void;
    onDrawRemove?: () => void;
    initialMode?: string;
    DrawClass: typeof MapboxDraw;
    options?: MapboxDraw.MapboxDrawOptions;
};

export default function DrawControl(props: DrawControlProps) {
    const { current: map } = useMap();

    const draw = useControl<MapboxDraw>(
        () => {
            if (!props.DrawClass) return null as unknown as MapboxDraw;
            console.log('DrawControl: Creating new MapboxDraw instance');
            console.log('DrawControl: Modes available:', !!props.DrawClass.modes);

            try {
                const drawInstance = new props.DrawClass({
                    displayControlsDefault: false,
                    controls: {
                        polygon: false,
                        trash: false
                    },
                    defaultMode: props.initialMode,
                    ...props.options
                });
                if (props.onDrawReady) props.onDrawReady(drawInstance);
                return drawInstance;
            } catch (err) {
                console.error('DrawControl: Error creating MapboxDraw instance:', err);
                return null as unknown as MapboxDraw;
            }
        },
        {
            position: props.position
        }
    );

    // Destructure the specific draw callbacks so the listener effect below depends on
    // their individual identities, not the whole `props` object.
    const { onCreate, onUpdate, onDelete, onModeChange, onDrawRemove } = props;

    useEffect(() => {
        if (!map || !draw) return;

        console.log('DrawControl: Attaching event listeners');

        if (onCreate) map.on('draw.create', onCreate);
        if (onUpdate) map.on('draw.update', onUpdate);
        if (onDelete) map.on('draw.delete', onDelete);
        if (onModeChange) map.on('draw.modechange', onModeChange);

        return () => {
            console.log('DrawControl: Cleaning up event listeners');
            if (onCreate) map.off('draw.create', onCreate);
            if (onUpdate) map.off('draw.update', onUpdate);
            if (onDelete) map.off('draw.delete', onDelete);
            if (onModeChange) map.off('draw.modechange', onModeChange);

            if (onDrawRemove) onDrawRemove();
        };
        // Depends on the SPECIFIC draw callbacks (not the whole `props` object):
        // listeners rebind only when one of these identities changes, not on every
        // render — rebinding every render would tear down a drawn polygon mid-interaction.
    }, [map, draw, onCreate, onUpdate, onDelete, onModeChange, onDrawRemove]);

    return null;
}
