'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { AssistantCalls } from '@/components/assistant-calls';
import { Header } from '@/components/header';
import { PermissionGate } from '@/components/PermissionGate';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import {
    getAssistantSettings, getGroups, setAssistantGroup, setAssistantUser, updateAssistantSettings,
    type AssistantSettings, type Group,
} from '@/lib/api';

// Settings → Assistant, for administrators (docs/ASSISTANT.md): whether the MCP server is on, who may use the
// assistant, and whether new datasets start closed to it. Each change is sent at once; the server answers the whole
// settings, which replace what the page shows.

interface Person {
    id: number;
    username: string;
    role: 'admin' | 'power_user' | 'guest';
}

// The roles ticked here. Guests aren't among them: a guest never may use the MCP server (the server keeps their
// setting for the chat in the app).
const ROLE_LABELS: Record<Exclude<keyof AssistantSettings['roles'], 'guest'>, string> = {
    admin: 'Administrators',
    power_user: 'Power users',
};
const GUEST_REFUSED = "Guests can't use the MCP server";
type Answer = 'default' | 'allowed' | 'refused';
const ANSWER_LABELS: Record<Answer, string> = {
    default: 'Role and groups decide',
    allowed: 'May use it',
    refused: 'May not use it',
};

/** Whether `person` may use the MCP server by the settings (the server's rule, backend/assistant/rule.py), the switch
 * aside: never a guest; otherwise their own answer, else a listed group they are in, else their role. */
function mayUse(person: Person, settings: AssistantSettings, groups: Group[]): boolean {
    if (person.role === 'guest') return false;
    const own = settings.users.find(user => user.id === person.id);
    if (own) return own.allowed;
    const listed = new Set(settings.groups.map(group => group.id));
    if (groups.some(group => listed.has(group.id) && group.members.some(member => member.id === person.id))) return true;
    return settings.roles[person.role];
}

function Switch({ label, checked, disabled, onChange, describedBy }: {
    label: string; checked: boolean; disabled: boolean; onChange: (next: boolean) => void; describedBy?: string;
}) {
    return (
        <button type="button" role="switch" aria-checked={checked} aria-label={label} aria-describedby={describedBy}
            disabled={disabled} onClick={() => onChange(!checked)}
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${checked ? 'bg-cyan-600' : 'bg-zinc-700'}`}>
            <span className={`inline-block h-5 w-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
    );
}

function AssistantSettingsPanel() {
    const queryClient = useQueryClient();
    const settings = useQuery({ queryKey: ['assistant-settings'], queryFn: getAssistantSettings, retry: false, meta: { errorHandledLocally: true } });
    const groups = useQuery({ queryKey: ['groups'], queryFn: getGroups, retry: false, meta: { errorHandledLocally: true } });
    // Whom an answer can be given to: active people, not a follower's account nor a demo guest's (a demo account
    // never may use the assistant), as the group member picker lists them.
    const people = useQuery<Person[]>({
        queryKey: ['users', 'people'],
        queryFn: async () => {
            const res = await fetch('/api/users?people=true', { credentials: 'include' });
            if (!res.ok) throw new Error('Could not list the users');
            const list: Person[] = await res.json();
            return list.sort((a, b) => a.username.localeCompare(b.username));
        },
        retry: false,
        meta: { errorHandledLocally: true },
    });
    const [filter, setFilter] = useState('');
    const saved = (next: AssistantSettings) => queryClient.setQueryData(['assistant-settings'], next);
    const change = useMutation({ mutationFn: updateAssistantSettings, onSuccess: saved });
    const group = useMutation({ mutationFn: ({ id, listed }: { id: number; listed: boolean }) => setAssistantGroup(id, listed), onSuccess: saved });
    const user = useMutation({ mutationFn: ({ id, allowed }: { id: number; allowed: boolean | null }) => setAssistantUser(id, allowed), onSuccess: saved });
    const mutations = [change, group, user];
    const busy = mutations.some(mutation => mutation.isPending);
    // One error at a time: the one from the change just made.
    const act = (run: () => void) => { mutations.forEach(mutation => mutation.reset()); run(); };
    const error = mutations.find(mutation => mutation.error)?.error ?? settings.error;

    if (settings.isLoading) return <p className="text-zinc-500">Loading the assistant settings…</p>;
    const current = settings.data;
    if (!current) return error ? <p role="alert" className="text-sm text-red-400">{error.message}</p> : null;
    const listed = new Set(current.groups.map(each => each.id));
    const needle = filter.trim().toLowerCase();
    const shown = (people.data ?? []).filter(person => person.username.toLowerCase().includes(needle));

    return (
        <div className="space-y-10">
            {error && <p role="alert" className="text-sm text-red-400">{error.message}</p>}

            <section aria-labelledby="assistant-mcp-heading" className="space-y-2">
                <h2 id="assistant-mcp-heading" className="text-xl font-semibold text-white">MCP server</h2>
                <div className="flex items-center gap-3">
                    <Switch label="MCP server" checked={current.mcp_enabled} disabled={busy} describedBy="assistant-mcp-note"
                        onChange={next => act(() => change.mutate({ mcp_enabled: next }))} />
                    <span className="text-sm text-zinc-200">{current.mcp_enabled ? 'On' : 'Off'}</span>
                </div>
                <p id="assistant-mcp-note" className="text-sm text-zinc-400">
                    {current.mcp_enabled
                        ? 'An assistant such as Claude can connect to this server, as one of the people below, and read the datasets that person can open.'
                        : 'No assistant can connect to this server. Switch it on to let the people below use one.'}
                </p>
                {current.mcp_enabled && (
                    <p className="text-sm text-zinc-400">
                        To connect an AI client, <Link href="/settings/tokens?kind=mcp" className="text-cyan-400 hover:underline">create an MCP token</Link>.
                        Each person who may use it makes their own, under Settings → API Tokens.
                    </p>
                )}
            </section>

            <section aria-labelledby="assistant-who-heading" className="space-y-4">
                <div>
                    <h2 id="assistant-who-heading" className="text-xl font-semibold text-white">Who may use it</h2>
                    <p className="text-sm text-zinc-400">
                        A person&apos;s own answer decides first; otherwise being in one of the groups ticked here lets them in;
                        otherwise their role decides. A guest or a demo account never may. The assistant reads as the person using it: it
                        sees only the datasets they can open, and changes nothing.
                    </p>
                </div>
                <fieldset className="space-y-2" disabled={busy}>
                    <legend className="text-sm font-medium text-zinc-300 mb-1">Roles</legend>
                    {(Object.keys(ROLE_LABELS) as (keyof typeof ROLE_LABELS)[]).map(role => (
                        <label key={role} className="flex items-center gap-2 text-sm text-zinc-200">
                            <input type="checkbox" checked={current.roles[role]}
                                onChange={event => act(() => change.mutate({ roles: { [role]: event.target.checked } }))}
                                className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500" />
                            {ROLE_LABELS[role]}
                        </label>
                    ))}
                </fieldset>
                <fieldset className="space-y-2" disabled={busy}>
                    <legend className="text-sm font-medium text-zinc-300 mb-1">Groups whose members may use it</legend>
                    {groups.error && <p role="alert" className="text-sm text-red-400">{groups.error.message}</p>}
                    {groups.data?.length === 0 && (
                        <p className="text-sm text-zinc-500">No groups yet: make them under <Link href="/users" className="text-cyan-400 hover:underline">User Management</Link>.</p>
                    )}
                    <div className="flex flex-wrap gap-3">
                        {groups.data?.map(each => (
                            <label key={each.id} className="flex items-center gap-2 text-sm text-zinc-200 border border-zinc-800 rounded px-2 py-1">
                                <input type="checkbox" checked={listed.has(each.id)}
                                    onChange={event => act(() => group.mutate({ id: each.id, listed: event.target.checked }))}
                                    className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500" />
                                {each.name}
                            </label>
                        ))}
                    </div>
                </fieldset>
                <div className="space-y-2">
                    <h3 className="text-sm font-medium text-zinc-300">People</h3>
                    <input aria-label="Find a person" placeholder="Find a person" value={filter} onChange={event => setFilter(event.target.value)}
                        className="w-full max-w-sm bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-cyan-500" />
                    {people.error && <p role="alert" className="text-sm text-red-400">{people.error.message}</p>}
                    <table aria-label="People and the assistant" className="w-full text-left text-sm">
                        <thead className="text-zinc-400 border-b border-zinc-800">
                            <tr>
                                <th className="py-2 pr-4 font-medium">Person</th>
                                <th className="py-2 pr-4 font-medium">Role</th>
                                <th className="py-2 pr-4 font-medium">Their own answer</th>
                                <th className="py-2 font-medium">May use it</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-zinc-800">
                            {shown.map(person => {
                                const own = current.users.find(each => each.id === person.id);
                                const answer: Answer = own ? (own.allowed ? 'allowed' : 'refused') : 'default';
                                const guest = person.role === 'guest';
                                return (
                                    <tr key={person.id}>
                                        <td className="py-2 pr-4 text-white">{person.username}</td>
                                        <td className="py-2 pr-4 text-zinc-400">{person.role.replace('_', ' ')}</td>
                                        <td className="py-2 pr-4">
                                            <select aria-label={`Assistant for ${person.username}`} value={answer} disabled={busy || guest}
                                                title={guest ? GUEST_REFUSED : undefined}
                                                onChange={event => {
                                                    const next = event.target.value as Answer;
                                                    act(() => user.mutate({ id: person.id, allowed: next === 'default' ? null : next === 'allowed' }));
                                                }}
                                                className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-200 disabled:opacity-50">
                                                {(Object.keys(ANSWER_LABELS) as Answer[]).map(each => <option key={each} value={each}>{ANSWER_LABELS[each]}</option>)}
                                            </select>
                                        </td>
                                        <td className="py-2 text-zinc-300">{mayUse(person, current, groups.data ?? []) ? 'Yes' : 'No'}</td>
                                    </tr>
                                );
                            })}
                            {people.isSuccess && !shown.length && (
                                <tr><td colSpan={4} className="py-2 text-zinc-500">{needle ? `No person matches "${filter.trim()}".` : 'No people yet.'}</td></tr>
                            )}
                        </tbody>
                    </table>
                    {!current.mcp_enabled && <p className="text-xs text-zinc-500">While the MCP server is off, nobody can use it, whatever this column says.</p>}
                </div>
            </section>

            <section aria-labelledby="assistant-datasets-heading" className="space-y-2">
                <h2 id="assistant-datasets-heading" className="text-xl font-semibold text-white">Datasets</h2>
                <label className="flex items-center gap-2 text-sm text-zinc-200">
                    <input type="checkbox" checked={current.new_datasets_closed} disabled={busy}
                        onChange={event => act(() => change.mutate({ new_datasets_closed: event.target.checked }))}
                        className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500" />
                    New datasets start closed to the assistant
                </label>
                <p className="text-sm text-zinc-400">
                    A dataset&apos;s managers open or close it to the assistant in its Dataset Settings, and anyone who can edit it can
                    keep a field&apos;s values from the assistant there. Datasets that exist already are not changed by this setting.
                </p>
                <label className="flex items-center gap-2 text-sm text-zinc-200">
                    <input type="checkbox" checked={current.sample_fields} disabled={busy}
                        onChange={event => act(() => change.mutate({ sample_fields: event.target.checked }))}
                        className="rounded bg-zinc-800 border-zinc-700 text-cyan-600 focus:ring-cyan-500" />
                    The assistant may read samples&apos; fields
                </label>
                <p className="text-sm text-zinc-400">
                    Off, it gives a sample as its id and a link that opens it. On, it may also read the fields it asks for and the
                    file path, except fields whose values are kept from it.
                </p>
            </section>

            <AssistantCalls />
        </div>
    );
}

export default function AssistantSettingsPage() {
    return (
        <ProtectedRoute>
            <div className="min-h-screen bg-zinc-950 flex flex-col">
                <Header />
                <main className="flex-1 container mx-auto px-6 py-8 max-w-4xl">
                    <Link href="/settings" className="inline-flex items-center gap-1 text-sm text-zinc-400 hover:text-white mb-4">
                        <ArrowLeft size={14} /> Back to Settings
                    </Link>
                    <h1 className="text-3xl font-bold text-white">Assistant</h1>
                    <p className="text-zinc-400 mt-1 mb-8">
                        An AI assistant answers questions about the datasets on this server, reading them as the person who uses it.
                    </p>
                    <PermissionGate role="admin" fallback={<p className="text-zinc-400 text-sm">Only administrators can change the assistant&apos;s settings.</p>}>
                        <AssistantSettingsPanel />
                    </PermissionGate>
                </main>
            </div>
        </ProtectedRoute>
    );
}
