/** Dataset descriptions also carry the importer's data credits and license terms. */
export function DatasetDescription({ description }: { description?: string | null }) {
    if (!description?.trim()) return null;
    const links = description.match(/https?:\/\/[^\s]+/g) || [];
    return (
        <div data-testid="dataset-description" title={description} className="text-xs text-zinc-400 whitespace-normal break-words">
            <p data-testid="dataset-description-text" className="line-clamp-2">
                {description.replace(/https?:\/\/[^\s]+/g, '').trim()}
            </p>
            {links.map((url, i) => (
                <a key={i} href={url} target="_blank" rel="noopener noreferrer" className="block w-fit py-1 underline hover:text-zinc-200 focus-visible:text-zinc-200">
                    Data credits &amp; license{links.length > 1 ? ` (${i + 1})` : ''}
                </a>
            ))}
        </div>
    );
}
