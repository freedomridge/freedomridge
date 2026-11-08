import React, { useEffect, useRef, useState } from 'react';
import { FilterQueryProblem, formatFilterQuery, parseFilterQuery, type ViewStage } from '@/lib/api';
import { offsetOf, queryEditorText, stagesFromQuery } from '@/lib/filter-query';

interface FilterQueryEditorProps {
    datasetId: number;
    stages: ViewStage[];
    onApply: (stages: ViewStage[]) => void;
    onClose: () => void;
}

// The filters as text, in the filter bar (docs/FILTER_QUERY.md): read, copy, paste or edit them, then Apply. The
// server checks the text against the dataset's fields; a mistake is shown with its line and column, and nothing
// changes.
export function FilterQueryEditor({ datasetId, stages, onApply, onClose }: FilterQueryEditorProps) {
    const [text, setText] = useState<string | null>(null);  // null until the current filters are written out
    const [problem, setProblem] = useState<FilterQueryProblem | null>(null);
    const [failure, setFailure] = useState<string | null>(null);
    const [applying, setApplying] = useState(false);
    const textArea = useRef<HTMLTextAreaElement>(null);

    useEffect(() => {
        let cancelled = false;
        formatFilterQuery(datasetId, stages.map(stage => stage.filter))
            .then(({ parts }) => {
                if (cancelled) return;
                setText(queryEditorText(stages, parts));
            })
            .catch((error: unknown) => {
                if (cancelled) return;
                setFailure(`Could not write the current filters as text: ${error instanceof Error ? error.message : String(error)}`);
                setText('');
            });
        return () => { cancelled = true; };
        // Written once, when the editor opens: later chip changes must not overwrite what is being typed.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const apply = async () => {
        if (text == null || applying) return;
        setApplying(true);
        setProblem(null);
        setFailure(null);
        try {
            const current = stages.map(stage => stage.filter);
            // Which chips have no text form is decided against the chips as they are now.
            const [{ parts }, parsed] = await Promise.all([
                formatFilterQuery(datasetId, current),
                parseFilterQuery(datasetId, text, current),
            ]);
            onApply(stagesFromQuery(stages, parts, parsed));
            onClose();
        } catch (error) {
            if (error instanceof FilterQueryProblem) {
                setProblem(error);
                const at = offsetOf(text, error.line, error.column);
                textArea.current?.focus();
                textArea.current?.setSelectionRange(at, at);
            } else {
                setFailure(error instanceof Error ? error.message : String(error));
            }
        } finally {
            setApplying(false);
        }
    };

    return (
        <div data-testid="filter-query-editor" className="flex w-full flex-col gap-1.5">
            {text == null ? (
                <div className="h-16 animate-pulse rounded-lg bg-zinc-900" />
            ) : (
                <textarea
                    ref={textArea}
                    data-testid="filter-query-text"
                    autoFocus
                    spellCheck={false}
                    // Grows with the text, up to 12 lines; drag the corner for more.
                    rows={Math.min(12, Math.max(3, text.split('\n').length))}
                    value={text}
                    onChange={event => { setText(event.target.value); setProblem(null); }}
                    onKeyDown={event => {
                        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                            event.preventDefault();
                            void apply();
                        } else if (event.key === 'Escape') {
                            event.preventDefault();
                            onClose();
                        }
                    }}
                    placeholder={'serial in (103000752 103001025)\nAND split = "train"'}
                    className="w-full resize-y rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs leading-relaxed text-zinc-200 placeholder-zinc-600 outline-none focus:border-cyan-500"
                />
            )}
            {problem && (
                <div data-testid="filter-query-problem" className="text-xs text-red-300">
                    Line {problem.line}, column {problem.column}: {problem.message}
                    {problem.suggestions.length > 0 && ` (did you mean ${problem.suggestions.map(name => `'${name}'`).join(' or ')}?)`}
                </div>
            )}
            {failure && <div data-testid="filter-query-failure" className="text-xs text-red-300">{failure}</div>}
            <div className="flex items-center justify-between gap-3">
                <span className="text-[11px] text-zinc-500">
                    = != &gt; &lt; · in (a b c) · contains · between · exists · is null · AND OR NOT ( ) ·
                    Each top-level AND part is one filter. Values in a list can be separated by spaces, so a pasted
                    column works. Ctrl+Enter applies, Esc cancels.
                </span>
                <div className="flex shrink-0 gap-2">
                    <button
                        type="button"
                        onClick={onClose}
                        className="rounded px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        data-testid="filter-query-apply"
                        onClick={() => void apply()}
                        disabled={text == null || applying}
                        className="rounded bg-cyan-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-cyan-500 disabled:opacity-50"
                    >
                        {applying ? 'Applying…' : 'Apply'}
                    </button>
                </div>
            </div>
        </div>
    );
}
