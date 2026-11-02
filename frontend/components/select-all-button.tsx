import { useId } from 'react';

export const SELECT_ALL_TOKEN_THRESHOLD = 50_000;
export const GUEST_SELECT_ALL_MESSAGE = 'Select All above 50,000 requires a power user. Browse, filter, export or save this view without selecting all.';

export function SelectAllButton({ total, guest, onSelectAll }: {
    total: number | null;
    guest: boolean;
    onSelectAll: () => void;
}) {
    const helpId = useId();
    const restricted = guest && total !== null && total > SELECT_ALL_TOKEN_THRESHOLD;
    return (
        <div className="group relative flex items-center" tabIndex={restricted ? 0 : undefined} title={restricted ? GUEST_SELECT_ALL_MESSAGE : undefined}>
            <button
                onClick={onSelectAll}
                disabled={restricted || (guest && total === null)}
                aria-describedby={restricted ? helpId : undefined}
                data-testid="grid-select-all"
                className="text-xs px-2 py-1 text-zinc-300 hover:text-white hover:bg-zinc-800 rounded transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed"
            >
                Select All{total ? ` (${total.toLocaleString()})` : ''}
            </button>
            {restricted && <span id={helpId} role="tooltip" className="absolute right-0 top-full z-50 hidden w-64 rounded bg-zinc-900 p-2 text-xs text-zinc-300 shadow-lg group-hover:block group-focus-within:block">{GUEST_SELECT_ALL_MESSAGE}</span>}
        </div>
    );
}
