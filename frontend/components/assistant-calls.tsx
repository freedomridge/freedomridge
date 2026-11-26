'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getAssistantCalls } from '@/lib/api';
import { CALLS_PAGE, callDoor, callDuration, callOutcome, callPerson, callsShowing } from '@/lib/assistant-calls';

/** Settings → Assistant's "Recent calls": the assistant's call log, newest first, a page at a time (admins only). */
export function AssistantCalls() {
    const [offset, setOffset] = useState(0);
    const calls = useQuery({
        queryKey: ['assistant-calls', offset],
        queryFn: () => getAssistantCalls(CALLS_PAGE, offset),
        retry: false,
        meta: { errorHandledLocally: true },
    });
    const page = calls.data;

    return (
        <section aria-labelledby="assistant-calls-heading" className="space-y-2">
            <div className="flex items-center justify-between gap-4">
                <h2 id="assistant-calls-heading" className="text-xl font-semibold text-white">Recent calls</h2>
                <button type="button" onClick={() => calls.refetch()} disabled={calls.isFetching}
                    className="px-3 py-1 text-sm border border-zinc-700 rounded text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
                    Refresh
                </button>
            </div>
            <p className="text-sm text-zinc-400">
                The tool calls made through the assistant in the last 30 days, newest first: who, which tool and dataset, how
                long each took and how it ended. What was asked and what was answered are not kept.
            </p>
            {calls.error && <p role="alert" className="text-sm text-red-400">{calls.error.message}</p>}
            {calls.isLoading && <p className="text-sm text-zinc-500">Loading the recent calls…</p>}
            {page && (
                <>
                    <table aria-label="Recent assistant calls" className="w-full text-left text-sm">
                        <thead className="text-zinc-400 border-b border-zinc-800">
                            <tr>
                                <th className="py-2 pr-4 font-medium">When</th>
                                <th className="py-2 pr-4 font-medium">Person</th>
                                <th className="py-2 pr-4 font-medium">Door</th>
                                <th className="py-2 pr-4 font-medium">Tool</th>
                                <th className="py-2 pr-4 font-medium">Dataset</th>
                                <th className="py-2 pr-4 font-medium">Took</th>
                                <th className="py-2 font-medium">Result</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-zinc-800">
                            {page.calls.map(call => (
                                <tr key={call.id}>
                                    <td className="py-2 pr-4 text-zinc-300 whitespace-nowrap">{new Date(call.started_at).toLocaleString()}</td>
                                    <td className={`py-2 pr-4 ${call.username ? 'text-white' : 'text-zinc-500'}`}>{callPerson(call)}</td>
                                    <td className="py-2 pr-4 text-zinc-300">{callDoor(call.door)}</td>
                                    <td className="py-2 pr-4 font-mono text-zinc-200">{call.tool}</td>
                                    <td className="py-2 pr-4 text-zinc-300">{call.dataset_id ?? '—'}</td>
                                    <td className="py-2 pr-4 text-zinc-300 whitespace-nowrap">{callDuration(call.duration_ms)}</td>
                                    <td className={`py-2 ${call.ok ? 'text-zinc-300' : 'text-amber-400'}`}>{callOutcome(call)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                    <div className="flex items-center justify-between gap-4 text-sm text-zinc-400">
                        <p>{callsShowing(page, page.calls.length)}</p>
                        <div className="flex gap-2">
                            <button type="button" disabled={offset === 0 || calls.isFetching}
                                onClick={() => setOffset(Math.max(0, offset - CALLS_PAGE))}
                                className="px-3 py-1 border border-zinc-700 rounded text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
                                Newer
                            </button>
                            <button type="button" disabled={page.next_offset === null || calls.isFetching}
                                onClick={() => page.next_offset !== null && setOffset(page.next_offset)}
                                className="px-3 py-1 border border-zinc-700 rounded text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
                                Older
                            </button>
                        </div>
                    </div>
                </>
            )}
        </section>
    );
}
