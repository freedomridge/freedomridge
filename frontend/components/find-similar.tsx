import React, { useRef, useState } from 'react';
import { ImageIcon, MagnifyingGlassIcon } from '@radix-ui/react-icons';
import * as Popover from '@radix-ui/react-popover';

export type ImageSearchInput = { imageBase64?: string; imageUrl?: string; label: string };
// Where a search looks: within the applied filters (they stay), or the whole dataset (it replaces them).
export type SearchScope = 'filtered' | 'all';

export const DEFAULT_SEARCH_LIMIT = 100;
const MAX_SEARCH_LIMIT = 50000;
export const SEARCH_LIMIT_CHOICES = [50, 100, 500, 1000, 5000, 10000, 50000];
export const SEARCH_LIMIT_TITLE = 'How many of the closest images a search shows. Up to 1,000 is fast (indexed); more runs an exact scan.';

/** The results count a search asks for: a whole number from 1 to 50,000, else the default. */
export function searchLimitOf(value: number): number {
    return Number.isFinite(value) && value > 0 ? Math.min(Math.round(value), MAX_SEARCH_LIMIT) : DEFAULT_SEARCH_LIMIT;
}

// Within your filters | Whole dataset.
function ScopeSwitch({ scope, onScopeChange }: { scope: SearchScope; onScopeChange: (scope: SearchScope) => void }) {
    return (
        <div role="group" aria-label="Search where" className="flex shrink-0 rounded-full border border-zinc-700 p-0.5 text-[11px] font-medium">
            {([['filtered', 'Within your filters'], ['all', 'Whole dataset']] as const).map(([value, label]) => (
                <button
                    key={value}
                    type="button"
                    data-testid={`search-scope-${value}`}
                    aria-pressed={scope === value}
                    onClick={() => onScopeChange(value)}
                    title={value === 'filtered'
                        ? 'Search only the images your filters show; the filters stay'
                        : 'Search every image in the dataset; the results replace your filters'}
                    className={`rounded-full px-2.5 py-1 transition-colors ${scope === value ? 'bg-zinc-700 text-white' : 'text-zinc-400 hover:text-zinc-200'}`}
                >
                    {label}
                </button>
            ))}
        </div>
    );
}

interface FindSimilarProps {
    // Natural-language (CLIP) search; absent when the dataset has no image-grain embedding field to search.
    onTextSearch?: (prompt: string) => void;
    // Reverse image search: an uploaded image or an image URL. Shares the text search's gate.
    onImageSearch?: (input: ImageSearchInput) => void;
    // A search is in flight: the button says so, and the inputs wait.
    isSearching: boolean;
    allowImageUrl: boolean;
    // How many of the closest images to show, shared with the search's chip.
    limit: number;
    onLimitChange: (limit: number) => void;
    // Within the applied filters or the whole dataset; the choice shows only when there are filters to search within.
    scope: SearchScope;
    onScopeChange: (scope: SearchScope) => void;
    hasFilters: boolean;
}

// The filter bar's Find similar button: a panel that says what the search does and takes a description or an
// example image. The results show as a chip in the filter bar.
export function FindSimilar({ onTextSearch, onImageSearch, isSearching, allowImageUrl, limit, onLimitChange, scope, onScopeChange, hasFilters }: FindSimilarProps) {
    const [open, setOpen] = useState(false);
    const [text, setText] = useState('');
    const [imageUrl, setImageUrl] = useState('');
    const fileInput = useRef<HTMLInputElement | null>(null);

    if (!onTextSearch && !onImageSearch) return null;

    const searchText = () => {
        if (!onTextSearch || !text.trim() || isSearching) return;
        onTextSearch(text.trim());
        setText('');
        setOpen(false);
    };
    const searchImage = (input: ImageSearchInput) => {
        onImageSearch?.(input);
        setImageUrl('');
        setOpen(false);
    };
    const searchFile = (file: File) => {
        const reader = new FileReader();
        // readAsDataURL gives a `data:image/...;base64,...` string the server accepts directly.
        reader.onload = () => searchImage({ imageBase64: String(reader.result), label: file.name });
        reader.readAsDataURL(file);
    };

    return (
        <Popover.Root open={open} onOpenChange={setOpen}>
            <Popover.Trigger asChild>
                <button
                    data-testid="search-open"
                    title="Find the images most like a description or an example image"
                    className={`flex shrink-0 items-center gap-1.5 rounded border px-3 py-2 text-xs font-medium transition-colors ${open || isSearching
                        ? 'border-cyan-600 bg-zinc-900 text-cyan-300'
                        : 'border-zinc-700 text-zinc-300 hover:border-zinc-600 hover:text-white'
                    }`}
                >
                    {isSearching ? (
                        <span data-testid="search-in-progress" className="flex items-center gap-1.5 text-amber-300">
                            <span className="h-3 w-3 animate-spin rounded-full border-2 border-amber-300 border-t-transparent" />
                            Searching…
                        </span>
                    ) : (
                        <>
                            <MagnifyingGlassIcon className="h-4 w-4" />
                            <span>Find similar</span>
                        </>
                    )}
                </button>
            </Popover.Trigger>
            <Popover.Portal>
                <Popover.Content sideOffset={6} align="start" className="z-[100] w-[460px] max-w-[90vw] space-y-4 rounded-xl border border-zinc-800 bg-zinc-950 p-4 shadow-2xl">
                    <div>
                        <div className="text-sm font-semibold text-zinc-100">Find similar images</div>
                        <p className="mt-1 text-xs text-zinc-500">
                            Ranks the images by how much they look like what you give it and shows the closest, best first.
                            The search shows as a chip in the filter bar; remove the chip to go back.
                        </p>
                    </div>
                    {onTextSearch && (
                        <div className="space-y-1.5">
                            <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">Describe it</div>
                            <div className="flex gap-2">
                                <input
                                    data-testid="semantic-search-input"
                                    autoFocus
                                    className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-cyan-500"
                                    placeholder='e.g. "corn with yellow leaves" or "a dirt road"'
                                    value={text}
                                    disabled={isSearching}
                                    onChange={e => setText(e.target.value)}
                                    onKeyDown={e => { if (e.key === 'Enter') searchText(); }}
                                />
                                <button
                                    onClick={searchText}
                                    disabled={isSearching || !text.trim()}
                                    className="rounded-lg bg-cyan-600 px-3 py-2 text-sm font-medium text-white hover:bg-cyan-500 disabled:opacity-40"
                                >
                                    Search
                                </button>
                            </div>
                        </div>
                    )}
                    {onImageSearch && (
                        <div className="space-y-1.5">
                            <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">Or use an example image</div>
                            <input
                                ref={fileInput}
                                type="file"
                                accept="image/*"
                                className="hidden"
                                data-testid="image-search-file"
                                onChange={e => {
                                    const file = e.target.files?.[0];
                                    if (file) searchFile(file);
                                    e.target.value = '';
                                }}
                            />
                            <div className="flex gap-2">
                                <button
                                    onClick={() => fileInput.current?.click()}
                                    disabled={isSearching}
                                    className="flex shrink-0 items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
                                >
                                    <ImageIcon className="h-4 w-4" /> Upload…
                                </button>
                                {allowImageUrl && (
                                    <input
                                        data-testid="image-search-url"
                                        className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-cyan-500"
                                        placeholder="or paste an image URL and press Enter"
                                        value={imageUrl}
                                        disabled={isSearching}
                                        onChange={e => setImageUrl(e.target.value)}
                                        onKeyDown={e => {
                                            if (e.key === 'Enter' && imageUrl.trim()) searchImage({ imageUrl: imageUrl.trim(), label: imageUrl.trim() });
                                        }}
                                    />
                                )}
                            </div>
                        </div>
                    )}
                    {hasFilters && (
                        <div className="flex items-center gap-2 border-t border-zinc-800 pt-3 text-[11px] text-zinc-500">
                            <ScopeSwitch scope={scope} onScopeChange={onScopeChange} />
                            <span>{scope === 'all' ? 'The results replace your filters' : 'Your filters stay'}</span>
                        </div>
                    )}
                    <div className="flex items-center gap-2 border-t border-zinc-800 pt-3 text-xs text-zinc-400" title={SEARCH_LIMIT_TITLE}>
                        Show the closest
                        <input
                            data-testid="search-result-limit"
                            type="number"
                            min={1}
                            max={MAX_SEARCH_LIMIT}
                            value={limit}
                            disabled={isSearching}
                            onChange={e => onLimitChange(Number(e.target.value))}
                            className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-cyan-500 disabled:opacity-50"
                        />
                        images
                        <span className="ml-auto text-[11px] text-zinc-600">up to 1,000 is instant</span>
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
