'use client';

import React, { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';

interface CreateUserModalProps {
    isOpen: boolean;
    onClose: () => void;
}

export function CreateUserModal({ isOpen, onClose }: CreateUserModalProps) {
    const queryClient = useQueryClient();
    const [username, setUsername] = useState('');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [role, setRole] = useState('guest');
    const [error, setError] = useState('');

    const mutation = useMutation({
        mutationFn: async () => {
            const res = await fetch('/api/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, email, password, role })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to create user');
            }
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['users'] });
            onClose();
            setUsername('');
            setEmail('');
            setPassword('');
            setRole('guest');
            setError('');
        },
        onError: (err: Error) => {
            setError(err.message);
        }
    });

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        mutation.mutate();
    };

    return (
        <Dialog.Root open={isOpen} onOpenChange={onClose}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-zinc-900 border border-zinc-800 p-6 rounded-lg shadow-xl w-full max-w-md z-50 focus:outline-none">
                    <div className="flex justify-between items-center mb-6">
                        <Dialog.Title className="text-xl font-semibold text-white">Create New User</Dialog.Title>
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

                    <form onSubmit={handleSubmit} className="space-y-4">
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Username</label>
                            <input
                                type="text"
                                value={username}
                                onChange={(e) => setUsername(e.target.value)}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-cyan-500 transition-colors"
                                required
                            />
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Email</label>
                            <input
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-cyan-500 transition-colors"
                                required
                            />
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Initial Password</label>
                            <input
                                type="password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-cyan-500 transition-colors"
                                required
                                minLength={8}
                            />
                            <p className="mt-1 text-xs text-zinc-500">User will be forced to change this on first login.</p>
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Role</label>
                            <select
                                value={role}
                                onChange={(e) => setRole(e.target.value)}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-cyan-500 transition-colors"
                            >
                                <option value="guest">Guest</option>
                                <option value="power_user">Power User</option>
                                <option value="admin">Admin</option>
                            </select>
                        </div>

                        <div className="flex justify-end gap-3 mt-6">
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
                                className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {mutation.isPending ? 'Creating...' : 'Create User'}
                            </button>
                        </div>
                    </form>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
