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
import { ApiError, createGroup, deleteGroup, getGroups, renameGroup, setGroupMember, type Group } from '@/lib/api';
import { levelLabel } from '@/lib/access-level';

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

                        <GroupsSection />
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

// How many member additions one Add has in flight at once (one request per person).
const ADDS_IN_FLIGHT = 6;

// Groups: a dataset shared with a group (Dataset Settings → Access) gives each member the group's level there.
// Administrators make them and choose their members here.
function GroupsSection() {
    const queryClient = useQueryClient();
    // Whom a group can hold: active people, not a follower's account nor a demo guest's (as a dataset's share picker).
    // One request for the page, shared by every group's checklist; by name, to find people in a long list.
    const { data: people = [], isSuccess: peopleLoaded, error: peopleError } = useQuery<User[]>({
        queryKey: ['users', 'people'],
        queryFn: async () => {
            const res = await fetch('/api/users?people=true');
            if (!res.ok) {
                // The server's words, shown in an open checklist. With the status, a 4xx isn't retried.
                let detail = 'Failed to fetch users';
                try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
                throw new ApiError(res.status, detail);
            }
            const list: User[] = await res.json();
            return list.sort((a, b) => a.username.localeCompare(b.username));
        },
    });
    const [newName, setNewName] = useState('');
    const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null);
    // Each group's open "Add members" checklist: its filter, and who is ticked (still ticked while filtered out of view).
    // A closed checklist has no entry.
    const [adding, setAdding] = useState<Record<number, { filter: string; ticked: number[] }>>({});
    const edit = (group: number, change: Partial<{ filter: string; ticked: number[] }>) =>
        setAdding(previous => (previous[group] ? { ...previous, [group]: { ...previous[group], ...change } } : previous));
    const close = (group: number) => {
        setAdding(previous => { const next = { ...previous }; delete next[group]; return next; });
        document.getElementById(`add-members-${group}`)?.focus();
    };
    const groups = useQuery({ queryKey: ['groups'], queryFn: getGroups, retry: false, meta: { errorHandledLocally: true } });
    const done = () => queryClient.invalidateQueries({ queryKey: ['groups'] });
    const create = useMutation({ mutationFn: createGroup, onSuccess: () => { setNewName(''); done(); } });
    const rename = useMutation({
        mutationFn: ({ id, name }: { id: number; name: string }) => renameGroup(id, name),
        onSuccess: () => { setRenaming(null); done(); },
    });
    const remove = useMutation({ mutationFn: deleteGroup, onSuccess: done });
    const member = useMutation({
        mutationFn: ({ group, user }: { group: number; user: number }) => setGroupMember(group, user, false),
        onSuccess: done,
    });
    // Everyone ticked, one request each, a few at a time (each holds a database connection while it runs). All added: the
    // checklist closes. Otherwise who was added shows as a member, and who wasn't stays ticked and is named in the error
    // (in the group's card), with the server's words.
    const addMembers = useMutation({
        mutationFn: async ({ group, users }: { group: number; users: User[] }) => {
            const reasons: string[] = [];
            let next = 0;
            await Promise.all(Array.from({ length: Math.min(ADDS_IN_FLIGHT, users.length) }, async () => {
                while (next < users.length) {
                    const index = next++;
                    try {
                        await setGroupMember(group, users[index].id, true);
                    } catch (failure) {
                        reasons[index] = failure instanceof Error ? failure.message : String(failure);
                    }
                }
            }));
            const failed = users.flatMap((user, index) => (index in reasons ? [{ user, reason: reasons[index] }] : []));
            if (failed.length) {
                throw Object.assign(
                    new Error(`Could not add ${failed.map(({ user, reason }) => `${user.username}: ${reason}`).join('; ')}`),
                    { notAdded: failed.map(({ user }) => user.id) },
                );
            }
        },
        onSuccess: (_, { group }) => close(group),
        onError: (failure, { group }) => {
            const notAdded = (failure as Error & { notAdded?: number[] }).notAdded;
            if (notAdded) edit(group, { ticked: notAdded });
        },
        onSettled: done,
    });
    const mutations = [create, rename, remove, member, addMembers];
    // An add's error shows in its group's card, next to its checklist; the others here.
    const error = [create, rename, remove, member].find(mutation => mutation.error)?.error ?? groups.error;
    // One error at a time: the one from the action just taken.
    const act = (run: () => void) => { mutations.forEach(mutation => mutation.reset()); run(); };
    const busy = mutations.some(mutation => mutation.isPending);
    return (
        <section aria-labelledby="groups-heading" className="mt-12">
            <h2 id="groups-heading" className="text-2xl font-bold text-white mb-1">Groups</h2>
            <p className="text-zinc-400">
                Share a dataset with a group in its Dataset Settings: each member gets the group&apos;s level there.
            </p>
            <p className="text-zinc-400 mb-4">
                To change a group&apos;s level in a dataset or remove its share, use the Access section in that dataset&apos;s Dataset Settings.
            </p>
            <form className="flex gap-2 mb-4" onSubmit={event => { event.preventDefault(); if (newName.trim()) act(() => create.mutate(newName.trim())); }}>
                <input
                    aria-label="New group name"
                    placeholder="New group name"
                    value={newName}
                    onChange={event => setNewName(event.target.value)}
                    className="flex-1 max-w-sm bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500"
                />
                <button type="submit" disabled={busy || !newName.trim()}
                    className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-md font-medium flex items-center gap-2 transition-colors disabled:opacity-50">
                    <PlusIcon className="w-4 h-4" />
                    Create Group
                </button>
            </form>
            {error && <p role="alert" className="mb-4 text-sm text-red-400">{error.message}</p>}
            {groups.isLoading ? (
                <div className="text-zinc-500">Loading groups...</div>
            ) : !groups.data?.length ? (
                !groups.isError && <div className="text-zinc-500">No groups yet.</div>
            ) : (
                <ul className="space-y-3">
                    {groups.data.map((group: Group) => {
                        const panel = adding[group.id];
                        const others = people.filter(user => !group.members.some(each => each.id === user.id));
                        const picked = panel ? others.filter(user => panel.ticked.includes(user.id)) : [];
                        const needle = panel?.filter.trim().toLowerCase() ?? '';
                        const shown = others.filter(user => user.username.toLowerCase().includes(needle));
                        const hidden = picked.filter(user => !shown.includes(user)).length;
                        const addError = addMembers.variables?.group === group.id ? addMembers.error : null;
                        return (
                            <li key={group.id} aria-label={`Group ${group.name}`} className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-4">
                                <div className="flex items-center gap-2">
                                    {renaming?.id === group.id ? (
                                        <form className="flex flex-1 gap-2" onSubmit={event => { event.preventDefault(); if (renaming.name.trim()) act(() => rename.mutate({ id: group.id, name: renaming.name.trim() })); }}>
                                            <input
                                                aria-label={`New name for group ${group.name}`}
                                                value={renaming.name}
                                                onChange={event => setRenaming({ id: group.id, name: event.target.value })}
                                                autoFocus
                                                className="flex-1 max-w-sm bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-cyan-500"
                                            />
                                            <button type="submit" disabled={busy || !renaming.name.trim()} className="px-3 py-1 text-sm bg-cyan-600 hover:bg-cyan-500 text-white rounded disabled:opacity-50">Save</button>
                                            <button type="button" onClick={() => setRenaming(null)} className="px-3 py-1 text-sm text-zinc-400 hover:text-white">Cancel</button>
                                        </form>
                                    ) : (
                                        <>
                                            <span className="flex-1 font-medium text-white">{group.name}</span>
                                            <button
                                                onClick={() => setRenaming({ id: group.id, name: group.name })}
                                                className="p-1.5 text-zinc-400 hover:text-white hover:bg-zinc-700 rounded transition-colors"
                                                aria-label={`Rename group ${group.name}`}
                                                title="Rename group"
                                            >
                                                <Pencil1Icon className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => {
                                                    if (confirm(`Delete the group "${group.name}"? Its members keep their accounts, and every dataset share the group has goes with it.`)) {
                                                        act(() => remove.mutate(group.id));
                                                    }
                                                }}
                                                disabled={busy}
                                                className="p-1.5 text-zinc-400 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors disabled:opacity-50"
                                                aria-label={`Delete group ${group.name}`}
                                                title="Delete group"
                                            >
                                                <TrashIcon className="w-4 h-4" />
                                            </button>
                                        </>
                                    )}
                                </div>
                                <ul aria-label={`Members of ${group.name}`} className="mt-3 flex flex-wrap gap-2">
                                    {group.members.map(each => (
                                        <li key={each.id} className="flex items-center gap-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-300">
                                            {each.username}
                                            <button
                                                onClick={() => act(() => member.mutate({ group: group.id, user: each.id }))}
                                                disabled={busy}
                                                className="text-zinc-500 hover:text-red-400 disabled:opacity-50"
                                                aria-label={`Remove ${each.username} from ${group.name}`}
                                                title={`Remove ${each.username} from the group`}
                                            >
                                                ×
                                            </button>
                                        </li>
                                    ))}
                                    {group.members.length === 0 && <li className="text-sm text-zinc-500">No members yet.</li>}
                                </ul>
                                {/* Escape closes this group's open checklist, as Cancel does, while the focus is in it. */}
                                <div className="mt-3" onKeyDown={event => { if (event.key === 'Escape' && panel && !busy) { event.stopPropagation(); close(group.id); } }}>
                                    <button
                                        id={`add-members-${group.id}`}
                                        onClick={() => panel ? close(group.id) : setAdding(previous => ({ ...previous, [group.id]: { filter: '', ticked: [] } }))}
                                        aria-expanded={!!panel}
                                        aria-controls={`add-members-panel-${group.id}`}
                                        aria-label={`Add members to ${group.name}`}
                                        className="px-3 py-1 text-sm bg-zinc-800 hover:bg-zinc-700 text-white rounded border border-zinc-700"
                                    >
                                        Add members
                                    </button>
                                    {panel && (
                                        <div id={`add-members-panel-${group.id}`} className="mt-2 space-y-2 border border-zinc-800 rounded p-3">
                                            <div className="flex items-center gap-3">
                                                <input
                                                    aria-label={`Find a user to add to ${group.name}`}
                                                    placeholder="Find a user"
                                                    value={panel.filter}
                                                    onChange={event => edit(group.id, { filter: event.target.value })}
                                                    autoFocus
                                                    className="flex-1 max-w-sm bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-cyan-500"
                                                />
                                                <span role="status" className="text-sm text-zinc-400">
                                                    {`${picked.length} selected${hidden ? `, ${hidden} hidden by the filter` : ''}`}
                                                </span>
                                            </div>
                                            {peopleError && <p role="alert" className="text-sm text-red-400">{peopleError.message}</p>}
                                            <div role="group" aria-label={`Users to add to ${group.name}`} className="flex flex-wrap gap-2 max-h-48 overflow-y-auto">
                                                {shown.map(user => (
                                                    <label key={user.id} className="flex items-center gap-1.5 border border-dashed border-zinc-700 rounded px-2 py-1 text-sm text-zinc-300 cursor-pointer hover:bg-zinc-800">
                                                        <input
                                                            type="checkbox"
                                                            checked={picked.includes(user)}
                                                            disabled={busy}
                                                            onChange={event => edit(group.id, {
                                                                ticked: event.target.checked
                                                                    ? [...panel.ticked, user.id]
                                                                    : panel.ticked.filter(id => id !== user.id),
                                                            })}
                                                            className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500"
                                                        />
                                                        {user.username}
                                                    </label>
                                                ))}
                                                {peopleLoaded && !others.length && <span className="py-1 text-sm text-zinc-500">No one else to add.</span>}
                                                {!!others.length && !shown.length && (
                                                    <span className="py-1 text-sm text-zinc-500">{`No user matches "${panel.filter.trim()}".`}</span>
                                                )}
                                            </div>
                                            <div className="flex gap-2">
                                                <button
                                                    onClick={() => act(() => addMembers.mutate({ group: group.id, users: picked }))}
                                                    disabled={busy || !picked.length}
                                                    className="px-3 py-1 text-sm bg-cyan-600 hover:bg-cyan-500 text-white rounded disabled:opacity-50"
                                                    aria-label={`Add the selected users to ${group.name}`}
                                                >
                                                    {picked.length ? `Add ${picked.length}` : 'Add'}
                                                </button>
                                                <button
                                                    onClick={() => close(group.id)}
                                                    disabled={busy}
                                                    className="px-3 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-50"
                                                    aria-label={`Cancel adding members to ${group.name}`}
                                                >
                                                    Cancel
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                    {addError && <p role="alert" className="mt-2 text-sm text-red-400">{addError.message}</p>}
                                </div>
                                <div className="mt-3 pt-3 border-t border-zinc-800 flex items-start gap-2">
                                    <span className="py-1 text-xs text-zinc-500">Datasets</span>
                                    <ul aria-label={`Datasets shared with ${group.name}`} className="flex flex-1 flex-wrap gap-2">
                                        {group.datasets.map(share => (
                                            <li key={share.id} className="flex items-center gap-2 bg-zinc-800/50 border border-zinc-800 rounded px-2 py-1 text-sm">
                                                <Link href={`/datasets/${share.id}`} className="text-cyan-400 hover:underline">{share.name}</Link>{' '}
                                                <span className="text-xs text-zinc-400">{levelLabel(share.level)}</span>
                                                {share.deleting && <>{' '}<span className="text-xs text-amber-400">(being deleted)</span></>}
                                            </li>
                                        ))}
                                        {group.datasets.length === 0 && <li className="py-1 text-sm text-zinc-500">Not shared with any dataset.</li>}
                                    </ul>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}
        </section>
    );
}
