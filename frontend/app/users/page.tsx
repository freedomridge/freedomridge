'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { Header } from '@/components/header';

import { PermissionGate } from '@/components/PermissionGate';
import { PlusIcon, TrashIcon, Pencil1Icon } from '@radix-ui/react-icons';
import { CreateUserModal } from '@/components/CreateUserModal';
import { EditUserModal } from '@/components/EditUserModal';
import { useUser } from '@/contexts/UserContext';

interface User {
    id: number;
    username: string;
    email: string;
    role: string;
    is_active: boolean;
    last_login: string | null;
}

export default function UsersPage() {
    const queryClient = useQueryClient();
    const { user: currentUser } = useUser();
    const [isCreateOpen, setIsCreateOpen] = useState(false);
    const [editingUser, setEditingUser] = useState<User | null>(null);

    const { data: users, isLoading } = useQuery<User[]>({
        queryKey: ['users'],
        queryFn: async () => {
            const res = await fetch('/api/users');
            if (!res.ok) throw new Error('Failed to fetch users');
            return res.json();
        }
    });

    const deleteMutation = useMutation({
        mutationFn: async (id: number) => {
            const res = await fetch(`/api/users/${id}`, { method: 'DELETE' });
            if (!res.ok) {
                // 409 = the user authored data and must be deactivated instead; show the server's reason.
                let detail = 'Failed to delete user';
                try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
                throw new Error(detail);
            }
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['users'] });
        },
        onError: (err: Error) => {
            alert(err.message);
        },
    });

    return (
        <ProtectedRoute>
            <PermissionGate role="admin" fallback={<div className="p-8 text-center text-zinc-400">Access Denied</div>}>
                <div className="min-h-screen bg-zinc-950 text-zinc-200 flex flex-col">
                    <Header />

                    <main className="flex-1 p-8 max-w-6xl mx-auto w-full">
                        <div className="mb-8">
                            <Link href="/settings" className="text-zinc-400 hover:text-white transition-colors text-sm mb-4 inline-block">
                                ← Back to Settings
                            </Link>
                            <div className="flex justify-between items-center">
                                <div>
                                    <h1 className="text-3xl font-bold text-white mb-2">User Management</h1>
                                    <p className="text-zinc-400">Manage system access and roles</p>
                                </div>
                                <button
                                    onClick={() => setIsCreateOpen(true)}
                                    className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-md font-medium flex items-center gap-2 transition-colors"
                                >
                                    <PlusIcon className="w-4 h-4" />
                                    Add User
                                </button>
                            </div>
                        </div>

                        {isLoading ? (
                            <div className="text-center py-12 text-zinc-500">Loading users...</div>
                        ) : (
                            <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg overflow-hidden">
                                <table className="w-full text-left text-sm">
                                    <thead className="bg-zinc-900 text-zinc-400 border-b border-zinc-800">
                                        <tr>
                                            <th className="px-6 py-3 font-medium">Username</th>
                                            <th className="px-6 py-3 font-medium">Email</th>
                                            <th className="px-6 py-3 font-medium">Role</th>
                                            <th className="px-6 py-3 font-medium">Last Active</th>
                                            <th className="px-6 py-3 font-medium text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-zinc-800">
                                        {users?.map((user) => (
                                            <tr key={user.id} className={`hover:bg-zinc-800/30 transition-colors ${!user.is_active ? 'opacity-50' : ''}`}>
                                                <td className="px-6 py-4 font-medium text-white">{user.username}</td>
                                                <td className="px-6 py-4 text-zinc-400">{user.email}</td>
                                                <td className="px-6 py-4">
                                                    <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${user.role === 'admin' ? 'bg-purple-500/10 text-purple-400' :
                                                        user.role === 'power_user' ? 'bg-cyan-500/10 text-cyan-400' :
                                                            'bg-zinc-700/30 text-zinc-400'
                                                        }`}>
                                                        {user.role.replace('_', ' ')}
                                                    </span>
                                                </td>
                                                <td className="px-6 py-4">
                                                    {user.last_login ? (() => {
                                                        const diff = Date.now() - new Date(user.last_login).getTime();
                                                        const mins = Math.floor(diff / 60000);
                                                        const hours = Math.floor(diff / 3600000);
                                                        const days = Math.floor(diff / 86400000);
                                                        const isRecent = mins < 30;
                                                        return (
                                                            <span className={`inline-flex items-center gap-1.5 ${isRecent ? 'text-green-400' : 'text-zinc-500'}`}>
                                                                <span className={`w-1.5 h-1.5 rounded-full ${isRecent ? 'bg-green-400' : 'bg-zinc-600'}`} />
                                                                {mins < 1 ? 'Just now'
                                                                    : mins < 60 ? `${mins}m ago`
                                                                    : hours < 24 ? `${hours}h ago`
                                                                    : days < 30 ? `${days}d ago`
                                                                    : new Date(user.last_login).toLocaleDateString()
                                                                }
                                                            </span>
                                                        );
                                                    })() : (
                                                        <span className="text-zinc-600">Never</span>
                                                    )}
                                                </td>
                                                <td className="px-6 py-4 text-right">
                                                    <div className="flex justify-end gap-2">
                                                        {currentUser?.id !== user.id && <button
                                                            onClick={async () => {
                                                                const res = await fetch(`/api/users/${user.id}`, {
                                                                    method: 'PATCH',
                                                                    headers: { 'Content-Type': 'application/json' },
                                                                    credentials: 'include',
                                                                    body: JSON.stringify({ is_active: !user.is_active }),
                                                                });
                                                                if (res.ok) queryClient.invalidateQueries({ queryKey: ['users'] });
                                                            }}
                                                            className={`p-1.5 rounded transition-colors ${user.is_active
                                                                ? 'text-green-400 hover:text-red-400 hover:bg-red-900/20'
                                                                : 'text-red-400 hover:text-green-400 hover:bg-green-900/20'
                                                            }`}
                                                            title={user.is_active ? 'Disable account' : 'Enable account'}
                                                        >
                                                            {user.is_active ? (
                                                                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" /></svg>
                                                            ) : (
                                                                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                                                            )}
                                                        </button>}
                                                        <button
                                                            onClick={() => setEditingUser(user)}
                                                            className="p-1.5 text-zinc-400 hover:text-white hover:bg-zinc-700 rounded transition-colors"
                                                            title="Edit User"
                                                        >
                                                            <Pencil1Icon className="w-4 h-4" />
                                                        </button>
                                                        <button
                                                            onClick={() => {
                                                                if (confirm('Are you sure you want to delete this user?')) {
                                                                    deleteMutation.mutate(user.id);
                                                                }
                                                            }}
                                                            className="p-1.5 text-zinc-400 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors"
                                                            title="Delete User"
                                                        >
                                                            <TrashIcon className="w-4 h-4" />
                                                        </button>
                                                    </div>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </main>

                    {/* Modals */}
                    <CreateUserModal
                        isOpen={isCreateOpen}
                        onClose={() => setIsCreateOpen(false)}
                    />

                    <EditUserModal
                        user={editingUser}
                        onClose={() => setEditingUser(null)}
                    />
                </div>
            </PermissionGate>
        </ProtectedRoute>
    );
}
