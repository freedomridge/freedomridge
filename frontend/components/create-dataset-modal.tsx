'use client';

import { useState, type FormEvent } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { createDataset } from '@/lib/api';

export function CreateDatasetModal({ onClose }: { onClose: () => void }) {
    const queryClient = useQueryClient();
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const mutation = useMutation({
        mutationFn: () => createDataset({ name: name.trim(), description: description.trim() }),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['datasets'] });
            onClose();
        },
    });
    const close = () => { if (!mutation.isPending) onClose(); };
    const submit = (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (name.trim() && !mutation.isPending) mutation.mutate();
    };

    return <Dialog.Root open onOpenChange={open => { if (!open) close(); }}>
        <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50" />
            <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-zinc-900 border border-zinc-800 p-6 rounded-xl shadow-xl w-full max-w-md z-50 text-white">
                <div className="flex justify-between items-center mb-4">
                    <Dialog.Title className="text-xl font-semibold">Create Dataset</Dialog.Title>
                    <button type="button" onClick={close} disabled={mutation.isPending} aria-label="Close" className="text-zinc-400 hover:text-white disabled:opacity-50"><X size={20} /></button>
                </div>
                <Dialog.Description className="text-sm text-zinc-400 mb-4">
                    Create an empty, private dataset. Add samples using the SDK or ingestion tools.
                </Dialog.Description>
                <form onSubmit={submit} className="space-y-4">
                    <div>
                        <label htmlFor="dataset-name" className="block text-sm text-zinc-400 mb-1">Name</label>
                        <input id="dataset-name" value={name} onChange={event => setName(event.target.value)} required disabled={mutation.isPending} className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2" />
                    </div>
                    <div>
                        <label htmlFor="dataset-description" className="block text-sm text-zinc-400 mb-1">Description (optional)</label>
                        <textarea id="dataset-description" value={description} onChange={event => setDescription(event.target.value)} disabled={mutation.isPending} rows={3} className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2" />
                    </div>
                    {mutation.error && <p role="alert" className="text-sm text-red-400">{mutation.error.message}</p>}
                    <div className="flex justify-end gap-3">
                        <button type="button" onClick={close} disabled={mutation.isPending} className="px-4 py-2 rounded bg-zinc-800 disabled:opacity-50">Cancel</button>
                        <button type="submit" disabled={!name.trim() || mutation.isPending} className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50">{mutation.isPending ? 'Creating…' : 'Create Dataset'}</button>
                    </div>
                </form>
            </Dialog.Content>
        </Dialog.Portal>
    </Dialog.Root>;
}
