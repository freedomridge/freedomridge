'use client';

import React, { useState } from 'react';
import { useUser } from '@/contexts/UserContext';
import { useToast } from '@/contexts/ToastContext';
import { passwordChangeMessage } from '@/lib/password-reset';
import * as Dialog from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';

export function ChangePasswordModal({
    open,
    onOpenChange,
    forceRequired = false,
}: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    forceRequired?: boolean;
}) {
    const [currentPassword, setCurrentPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [revokeTokens, setRevokeTokens] = useState(false);
    const [error, setError] = useState('');
    const { changePassword } = useUser();
    const { addToast } = useToast();

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');

        if (newPassword !== confirmPassword) {
            setError("New passwords don't match");
            return;
        }

        if (newPassword.length < 8) {
            setError("Password must be at least 8 characters");
            return;
        }

        try {
            const revoked = await changePassword(currentPassword, newPassword, revokeTokens);
            onOpenChange(false);
            // The dialog closes: say what was revoked once it has.
            if (revokeTokens) addToast(passwordChangeMessage(revoked), 'success', 10000);
            // Clear form
            setCurrentPassword('');
            setNewPassword('');
            setConfirmPassword('');
            setRevokeTokens(false);
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : String(err));
        }
    };

    return (
        <Dialog.Root open={open} onOpenChange={onOpenChange}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed left-[50%] top-[50%] translate-x-[-50%] translate-y-[-50%] w-full max-w-md bg-zinc-900 rounded-xl p-6 border border-zinc-800 shadow-xl z-50 focus:outline-none">
                    <div className="flex items-center justify-between mb-4">
                        <Dialog.Title className="text-lg font-semibold text-white">
                            Change Password
                        </Dialog.Title>
                        {!forceRequired && (
                            <Dialog.Close className="text-zinc-400 hover:text-white">
                                <Cross2Icon />
                            </Dialog.Close>
                        )}
                    </div>

                    <form onSubmit={handleSubmit} className="space-y-4">
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">
                                Current Password
                            </label>
                            <input
                                type="password"
                                required
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:ring-2 focus:ring-cyan-500 focus:outline-none"
                                value={currentPassword}
                                onChange={(e) => setCurrentPassword(e.target.value)}
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">
                                New Password
                            </label>
                            <input
                                type="password"
                                required
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:ring-2 focus:ring-cyan-500 focus:outline-none"
                                value={newPassword}
                                onChange={(e) => setNewPassword(e.target.value)}
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">
                                Confirm New Password
                            </label>
                            <input
                                type="password"
                                required
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:ring-2 focus:ring-cyan-500 focus:outline-none"
                                value={confirmPassword}
                                onChange={(e) => setConfirmPassword(e.target.value)}
                            />
                        </div>
                        <div className="flex items-center gap-2">
                            <input
                                type="checkbox"
                                id="change_revoke_tokens"
                                checked={revokeTokens}
                                onChange={(e) => setRevokeTokens(e.target.checked)}
                                className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500"
                            />
                            <label htmlFor="change_revoke_tokens" className="text-sm text-zinc-300">Also revoke my API tokens</label>
                        </div>

                        {error && (
                            <div className="text-red-400 text-sm bg-red-900/20 p-2 rounded border border-red-900/50">
                                {error}
                            </div>
                        )}

                        <div className="flex justify-end gap-3 mt-6">
                            {!forceRequired && (
                                <Dialog.Close asChild>
                                    <button
                                        type="button"
                                        className="px-4 py-2 text-sm font-medium text-zinc-400 hover:text-white hover:bg-zinc-800 rounded transition-colors"
                                    >
                                        Cancel
                                    </button>
                                </Dialog.Close>
                            )}
                            <button
                                type="submit"
                                className="px-4 py-2 text-sm font-medium text-white bg-cyan-600 hover:bg-cyan-700 rounded transition-colors"
                            >
                                Change Password
                            </button>
                        </div>
                    </form>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
