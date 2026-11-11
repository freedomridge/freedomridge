'use client';

import React, { useState, useEffect } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
    DEACTIVATE_HINT,
    deactivationMessage,
    passwordResetMessage,
    RESET_PASSWORD_HINT,
    RESET_PASSWORD_KEEP_TOKENS_HINT,
    type RevokedTokens,
} from '@/lib/password-reset';

interface User {
    id: number;
    username: string;
    email: string;
    role: string;
    is_active: boolean;
}

interface EditUserModalProps {
    user: User | null;
    onClose: () => void;
}

export function EditUserModal({ user, onClose }: EditUserModalProps) {
    const queryClient = useQueryClient();
    const [role, setRole] = useState('guest');
    const [isActive, setIsActive] = useState(true);
    const [error, setError] = useState('');
    const [resetPassword, setResetPassword] = useState('');
    const [revokeTokens, setRevokeTokens] = useState(true);
    const [successMessage, setSuccessMessage] = useState('');

    useEffect(() => {
        if (user) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate prop-sync: reset the form fields when a different user prop is selected
            setRole(user.role);
            setIsActive(user.is_active);
            setResetPassword('');
            setRevokeTokens(true);
            setSuccessMessage('');
            setError('');
        }
    }, [user]);

    const updateMutation = useMutation({
        mutationFn: async () => {
            if (!user) return;
            const res = await fetch(`/api/users/${user.id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role, is_active: isActive })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to update user');
            }
            return (await res.json()) as { revoked_tokens?: RevokedTokens };
        },
        onSuccess: (answer) => {
            queryClient.invalidateQueries({ queryKey: ['users'] });
            // Deactivating revoked the account's API tokens: stay open to say how many.
            const revoked = answer?.revoked_tokens;
            if (revoked && (user?.is_active || Object.values(revoked).some(n => n))) {
                setSuccessMessage(deactivationMessage(revoked));
                return;
            }
            onClose();
        },
        onError: (err: Error) => {
            setError(err.message);
        }
    });

    const resetPasswordMutation = useMutation({
        mutationFn: async (revoke: boolean) => {
            if (!user || !resetPassword) return;
            const res = await fetch(`/api/users/${user.id}/reset-password`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: resetPassword, revoke_tokens: revoke })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to reset password');
            }
            return (await res.json()) as { revoked_tokens?: RevokedTokens };
        },
        onSuccess: (answer, revoke) => {
            setSuccessMessage(passwordResetMessage(answer?.revoked_tokens, revoke));
            setResetPassword('');
        },
        onError: (err: Error) => {
            setError(err.message);
        }
    });

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        updateMutation.mutate();
    };

    if (!user) return null;

    return (
        <Dialog.Root open={!!user} onOpenChange={() => onClose()}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-zinc-900 border border-zinc-800 p-6 rounded-lg shadow-xl w-full max-w-md z-50 focus:outline-none">
                    <div className="flex justify-between items-center mb-6">
                        <Dialog.Title className="text-xl font-semibold text-white">Edit User: {user.username}</Dialog.Title>
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

                    {successMessage && (
                        <div className="mb-4 p-3 bg-green-500/10 border border-green-500/20 rounded text-green-400 text-sm">
                            {successMessage}
                        </div>
                    )}

                    <form onSubmit={handleSubmit} className="space-y-6">
                        <div className="space-y-4">
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

                            <div className="flex items-center gap-2">
                                <input
                                    type="checkbox"
                                    id="is_active"
                                    checked={isActive}
                                    onChange={(e) => setIsActive(e.target.checked)}
                                    className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500"
                                />
                                <label htmlFor="is_active" className="text-sm font-medium text-zinc-300">Active Account</label>
                            </div>
                            {user.is_active && (
                                <p data-testid="deactivate-hint" className="text-xs text-zinc-500 -mt-2">{DEACTIVATE_HINT}</p>
                            )}
                        </div>

                        <div className="border-t border-zinc-800 pt-4">
                            <h3 className="text-sm font-medium text-zinc-400 mb-1">Reset Password</h3>
                            <p data-testid="reset-password-hint" className="text-xs text-zinc-500 mb-3">
                                {revokeTokens ? RESET_PASSWORD_HINT : RESET_PASSWORD_KEEP_TOKENS_HINT}
                            </p>
                            <div className="flex gap-2">
                                <input
                                    type="password"
                                    placeholder="New password"
                                    value={resetPassword}
                                    onChange={(e) => setResetPassword(e.target.value)}
                                    className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-cyan-500 transition-colors text-sm"
                                />
                                <button
                                    type="button"
                                    onClick={() => resetPasswordMutation.mutate(revokeTokens)}
                                    disabled={!resetPassword || resetPasswordMutation.isPending}
                                    className="px-3 py-2 bg-zinc-800 hover:bg-zinc-700 text-white rounded text-sm font-medium transition-colors disabled:opacity-50"
                                >
                                    Reset
                                </button>
                            </div>
                            <div className="flex items-center gap-2 mt-2">
                                <input
                                    type="checkbox"
                                    id="reset_revoke_tokens"
                                    checked={revokeTokens}
                                    onChange={(e) => setRevokeTokens(e.target.checked)}
                                    className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500"
                                />
                                <label htmlFor="reset_revoke_tokens" className="text-sm text-zinc-300">Also revoke their API tokens</label>
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
                                disabled={updateMutation.isPending}
                                className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded font-medium transition-colors disabled:opacity-50"
                            >
                                {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
                            </button>
                        </div>
                    </form>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
