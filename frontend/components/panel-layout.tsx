import React, { useState, useCallback, useEffect, useRef } from 'react';
import { Group, Panel, Separator } from 'react-resizable-panels';
import { PanelContent, PanelType } from './panel-content';

// --- Types ---

export type PanelId = string;

export type PanelNode =
    | { id: PanelId; type: 'leaf'; data: { panelType: PanelType }; size?: number }
    | { id: PanelId; type: 'group'; direction: 'horizontal' | 'vertical'; children: PanelNode[]; size?: number };

interface PanelLayoutProps {
    initialLayout?: PanelNode;
    renderPanel: (type: PanelType, id: string, setType: (type: PanelType) => void) => React.ReactNode;
    getIsActive?: (type: PanelType, id: string) => boolean;
}

// --- Helper Functions ---

function generateId(): string {
    return Math.random().toString(36).substr(2, 9) + Date.now().toString(36);
}

function findNode(node: PanelNode, id: PanelId): PanelNode | null {
    if (node.id === id) return node;
    if (node.type === 'group') {
        for (const child of node.children) {
            const found = findNode(child, id);
            if (found) return found;
        }
    }
    return null;
}

function replaceNode(root: PanelNode, id: PanelId, newNode: PanelNode): PanelNode {
    if (root.id === id) return newNode;
    if (root.type === 'group') {
        return {
            ...root,
            children: root.children.map(child => replaceNode(child, id, newNode))
        };
    }
    return root;
}

function removeNode(root: PanelNode, id: PanelId): PanelNode | null {
    if (root.id === id) return null; // Should not happen for root usually, or handled by caller
    if (root.type === 'group') {
        const newChildren = root.children
            .map(child => removeNode(child, id))
            .filter((child): child is PanelNode => child !== null);

        // If a group has only one child left, replace the group with the child
        if (newChildren.length === 1) {
            // CRITICAL FIX: The remaining child must inherit the size of the group it's replacing
            // to maintain the correct total size in the parent layout.
            return { ...newChildren[0], size: root.size };
        }

        return { ...root, children: newChildren };
    }
    return root;
}

function updateNodeSizes(root: PanelNode, id: PanelId, sizes: number[]): PanelNode {
    if (root.id === id && root.type === 'group') {
        return {
            ...root,
            children: root.children.map((child, index) => ({
                ...child,
                size: sizes[index]
            }))
        };
    }
    if (root.type === 'group') {
        return {
            ...root,
            children: root.children.map(child => updateNodeSizes(child, id, sizes))
        };
    }
    return root;
}

// --- Component ---

const LAYOUT_STORAGE_KEY = 'fridge-panel-layout-v2';

// Every panel type (the compiler checks the list is whole), so a kept layout naming another one opens on the default.
const PANEL_TYPES: Record<PanelType, true> = {
    grid: true, filters: true, empty: true, embeddings: true, map: true, histogram: true, duplicates: true, concepts: true,
    'sparse-sample': true,
};

// Whether a stored value is a layout this version can show: a panel of a known type, or a group of one or more of
// them. Anything else (damaged, or another version's shape) opens on the default layout.
function isPanelNode(value: unknown): value is PanelNode {
    if (typeof value !== 'object' || value === null) return false;
    const node = value as { id?: unknown; type?: unknown; size?: unknown; data?: unknown; direction?: unknown; children?: unknown };
    if (typeof node.id !== 'string' || (node.size !== undefined && typeof node.size !== 'number')) return false;
    if (node.type === 'leaf') {
        const panelType = (node.data as { panelType?: unknown } | null | undefined)?.panelType;
        return typeof panelType === 'string' && Object.hasOwn(PANEL_TYPES, panelType);
    }
    return node.type === 'group' && (node.direction === 'horizontal' || node.direction === 'vertical')
        && Array.isArray(node.children) && node.children.length > 0 && node.children.every(isPanelNode);
}

function readStoredLayout(): PanelNode | null {
    try {
        // A size stored as null (JSON's spelling of a NaN size) reads as no size.
        const noNullSize = (key: string, value: unknown) => (key === 'size' && value === null ? undefined : value);
        const parsed: unknown = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY) ?? 'null', noNullSize);
        return isPanelNode(parsed) ? parsed : null;
    } catch {
        return null;  // no storage, or not JSON: the default layout
    }
}

export function PanelLayout({ initialLayout, renderPanel, getIsActive }: PanelLayoutProps) {
    // Initialize with default layout matching user preference: Filters -> Grid -> Embeddings
    const defaultLayout: PanelNode = initialLayout || {
        id: 'root',
        type: 'group',
        direction: 'horizontal',
        children: [
            { id: 'left', type: 'leaf', data: { panelType: 'filters' }, size: 20 },
            { id: 'center', type: 'leaf', data: { panelType: 'grid' }, size: 50 },
            { id: 'right', type: 'leaf', data: { panelType: 'embeddings' }, size: 30 }
        ]
    };

    const [layout, setLayout] = useState<PanelNode>(defaultLayout);
    const [, setLayoutVersion] = useState(0);
    const [, setIsLoaded] = useState(false);

    // Use a ref to track the *current* layout including the latest sizes the person resized to.
    // This avoids re-rendering the entire tree on every resize. Every setLayout below sets it too.
    const layoutRef = useRef<PanelNode>(layout);

    // Load from local storage on mount
    useEffect(() => {
        if (typeof window !== 'undefined') {
            const saved = readStoredLayout();
            if (saved) {
                layoutRef.current = saved;
                // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
                setLayout(saved);
                setLayoutVersion(v => v + 1); // Force re-render of Group
            }
            setIsLoaded(true);
        }
    }, []);

    // Written only when the person changes the layout: opening the page never writes over the kept one.
    const saveLayout = useCallback((newLayout: PanelNode) => {
        try { localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(newLayout)); } catch { /* ignore */ }
    }, []);

    const handleSplit = useCallback((id: PanelId, direction: 'horizontal' | 'vertical') => {
        // Always work off the latest ref state to preserve current sizes
        const currentLayout = JSON.parse(JSON.stringify(layoutRef.current));

        const node = findNode(currentLayout, id);
        if (!node || node.type !== 'leaf') return;

        const newGroup: PanelNode = {
            id: generateId(),
            type: 'group',
            direction: direction,
            size: node.size, // Preserve size in parent
            children: [
                { ...node, size: 50, id: generateId() }, // Clone current node as first child
                { id: generateId(), type: 'leaf', data: { panelType: 'empty' }, size: 50 } // New empty node
            ]
        };

        const newLayout = replaceNode(currentLayout, id, newGroup);
        layoutRef.current = newLayout;
        setLayout(newLayout);
        setLayoutVersion(v => v + 1);
        saveLayout(newLayout);
    }, [saveLayout]);

    const handleClose = useCallback((id: PanelId) => {
        const currentLayout = JSON.parse(JSON.stringify(layoutRef.current));

        // Prevent closing the last panel if it's the root leaf
        if (currentLayout.type === 'leaf' && currentLayout.id === id) return;

        const newLayout = removeNode(currentLayout, id);
        if (newLayout) {
            layoutRef.current = newLayout;
            setLayout(newLayout);
            setLayoutVersion(v => v + 1);
            saveLayout(newLayout);
        }
    }, [saveLayout]);

    const handleTypeChange = useCallback((id: PanelId, newType: PanelType) => {
        const currentLayout = JSON.parse(JSON.stringify(layoutRef.current));
        const node = findNode(currentLayout, id);
        if (!node || node.type !== 'leaf') return;

        const newLayout = replaceNode(currentLayout, id, {
            ...node,
            data: { ...node.data, panelType: newType }
        });

        layoutRef.current = newLayout;
        setLayout(newLayout);
        setLayoutVersion(v => v + 1);
        saveLayout(newLayout);
    }, [saveLayout]);

    // A Group reports its sizes as it mounts too, and again when the kept layout replaces the default one: those
    // reports repeat the sizes the layout holds and are left alone. A resize the person makes (dragging a separator,
    // the keyboard, or a double click on a separator, which resets the panel next to it) is recorded and saved.
    const handleLayoutChanged = useCallback((groupId: string, sizes: (number | undefined)[], isUserInteraction: boolean) => {
        const group = findNode(layoutRef.current, groupId);
        // Not a layout of this group: the group is gone, or its panels are changing.
        if (!group || group.type !== 'group' || group.children.length !== sizes.length) return;
        if (!sizes.every((size): size is number => typeof size === 'number')) return;
        const children = group.children;
        const resized = sizes.some((size, index) => {
            const held = children[index].size;
            return held !== undefined && Math.abs(size - held) > 0.01;
        });
        if (!resized && !isUserInteraction) return;
        // Update the ref ONLY, do not trigger re-render
        layoutRef.current = updateNodeSizes(layoutRef.current, groupId, sizes);
        saveLayout(layoutRef.current);
    }, [saveLayout]);

    // Recursive renderer
    const renderNode = (node: PanelNode) => {
        if (node.type === 'leaf') {
            return (
                <PanelContent
                    title={
                        node.data.panelType === 'grid' ? 'Image Grid'
                            : node.data.panelType === 'filters' ? 'Simple Filters'
                                : node.data.panelType === 'embeddings' ? 'Embeddings'
                                    : node.data.panelType === 'map' ? 'Map'
                                        : node.data.panelType === 'histogram' ? 'Histogram'
                                            : node.data.panelType === 'duplicates' ? 'Duplicates'
                                                : 'Empty Panel'
                    }
                    type={node.data.panelType}
                    onSplit={(dir) => handleSplit(node.id, dir)}
                    onClose={() => handleClose(node.id)}
                    onTypeChange={(t) => handleTypeChange(node.id, t)}
                    isActive={getIsActive ? getIsActive(node.data.panelType, node.id) : false}
                >
                    {renderPanel(node.data.panelType, node.id, (t) => handleTypeChange(node.id, t))}
                </PanelContent>
            );
        }

        return (
            <Group
                // Force remount when children count changes OR layout version changes
                key={`${node.id}-${node.children.length}`}
                id={node.id}
                orientation={node.direction}
                onLayoutChanged={(sizes, { isUserInteraction }) => handleLayoutChanged(
                    node.id,
                    node.children.map(child => sizes[child.id]),
                    isUserInteraction,
                )}
            >
                {node.children.map((child, index) => (
                    <React.Fragment key={child.id}>
                        <Panel
                            id={child.id}
                            // Bare numbers are pixels; the stored sizes are percentages.
                            defaultSize={child.size === undefined ? undefined : `${child.size}%`}
                            minSize="10%"
                        >
                            {renderNode(child)}
                        </Panel>
                        {index < node.children.length - 1 && (
                            <Separator className={`flex items-center justify-center bg-zinc-900 hover:bg-cyan-500 transition-colors z-10 ${node.direction === 'horizontal' ? 'w-1.5 cursor-col-resize' : 'h-1.5 cursor-row-resize'}`}>
                                <div className={`bg-zinc-600 rounded-full ${node.direction === 'horizontal' ? 'w-0.5 h-4' : 'h-0.5 w-4'}`} />
                            </Separator>
                        )}
                    </React.Fragment>
                ))}
            </Group>
        );
    };

    return (
        <div className="h-full w-full">
            {/* eslint-disable-next-line react-hooks/refs -- renderNode only wires layoutRef into event handlers (onLayoutChange/onSplit/onClose/onTypeChange); the ref is read when those fire, not during render. It mirrors live panel sizes off-render to avoid re-rendering the tree every resize frame. */}
            {renderNode(layout)}
        </div>
    );
}
