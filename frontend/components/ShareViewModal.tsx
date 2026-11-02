'use client';

import React, { useState, useEffect } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { View, getUserNames, shareView } from '@/lib/api';

interface ShareViewModalProps {
    view: View | null;
    datasetId: number;
    onClose: () => void;
}

export function ShareViewModal({ view, datasetId, onClose }: ShareViewModalProps) {
    const queryClient = useQueryClient();
    const [isPublic, setIsPublic] = useState(false);
    const [sharedWith, setSharedWith] = useState<number[]>([]);
    const [error, setError] = useState('');

    // getUserNames, not getUsers: the full user list is admin-only and used to
    // 403 here for every non-admin owner (the error was swallowed and rendered
    // as "No other users found", silently breaking per-user sharing).
    const { data: users, error: usersError } = useQuery({
        queryKey: ['user-names'],
        queryFn: getUserNames
    });

    useEffect(() => {
        if (view) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate prop-sync: seed local sharing state from the selected view prop
            setIsPublic(view.is_public);
            setSharedWith(view.shared_with_user_ids || []);
        }
    }, [view]);

    const mutation = useMutation({
        mutationFn: async () => {
            if (!view) return;
            await shareView(datasetId, view.id, isPublic, sharedWith);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['views', datasetId] });
            onClose();
        },
        onError: (err: Error) => {
            setError(err.message);
        }
    });

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        mutation.mutate();
    };

    if (!view) return null;

    return (
        <Dialog.Root open={!!view} onOpenChange={() => onClose()}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-zinc-900 border border-zinc-800 p-6 rounded-lg shadow-xl w-full max-w-md z-50 focus:outline-none">
                    <div className="flex justify-between items-center mb-6">
                        <Dialog.Title className="text-xl font-semibold text-white">Share View: {view.name}</Dialog.Title>
                        <Dialog.Close asChild>
                            <button className="text-zinc-400 hover:text-white transition-colors">
                                <Cross2Icon className="w-4 h-4" />
                            </button>
                        </Dialog.Close>
                    </div>

                    {error && (
                        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/20 rounded text-red-400 text-sm">
                            {error}
                        </div>
                    )}

                    <form onSubmit={handleSubmit} className="space-y-6">
                        <div className="flex items-center gap-3 p-4 bg-zinc-800/50 rounded-lg border border-zinc-800">
                            <input
                                type="checkbox"
                                id="is_public"
                                checked={isPublic}
                                onChange={(e) => setIsPublic(e.target.checked)}
                                className="w-5 h-5 rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500"
                            />
                            <div>
                                <label htmlFor="is_public" className="text-sm font-medium text-white block">Make Public</label>
                                <p className="text-xs text-zinc-400">Anyone with access to this dataset can see this view.</p>
                            </div>
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-2">Share with specific users</label>
                            <div className="max-h-48 overflow-y-auto bg-zinc-800/30 border border-zinc-800 rounded-lg p-2 space-y-1">
                                {users?.map(user => (
                                    <label key={user.id} className="flex items-center gap-2 p-2 hover:bg-zinc-800 rounded cursor-pointer transition-colors">
                                        <input
                                            type="checkbox"
                                            checked={sharedWith.includes(user.id)}
                                            onChange={(e) => {
                                                if (e.target.checked) {
                                                    setSharedWith([...sharedWith, user.id]);
                                                } else {
                                                    setSharedWith(sharedWith.filter(id => id !== user.id));
                                                }
                                            }}
                                            className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500"
                                        />
                                        <span className="text-sm text-zinc-300">{user.username}</span>
                                    </label>
                                ))}
                                {usersError && (
                                    <p className="text-sm text-red-400 p-2">Failed to load users: {usersError.message}</p>
                                )}
                                {!usersError && (!users || users.length === 0) && (
                                    <p className="text-sm text-zinc-500 p-2">No other users found.</p>
                                )}
                            </div>
                        </div>

                        <div className="flex justify-end gap-3 pt-2">
                            <button
                                type="button"
                                onClick={onClose}
                                className="px-4 py-2 text-zinc-400 hover:text-white transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                type="submit"
                                disabled={mutation.isPending}
                                className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded font-medium transition-colors disabled:opacity-50"
                            >
                                {mutation.isPending ? 'Saving...' : 'Save Changes'}
                            </button>
                        </div>
                    </form>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
