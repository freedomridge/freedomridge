'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

// On-demand compute workers (docs/ON_DEMAND_WORKERS.md): the launcher's settings and the machines it started.
interface Settings {
    enabled: boolean;
    region: string;
    instance_type: string;
    subnet_id: string | null;
    security_group_ids: string[];
    server_url: string;
    image: string | null;
    access_key_id: string | null;
    has_secret: boolean;
    wait_minutes: number;
    idle_minutes: number;
    max_machines: number;
    max_hours_per_machine: number;
    monthly_hours: number;
    last_error: string | null;
    last_error_at: string | null;
}
interface Launch {
    id: number;
    instance_id: string | null;
    instance_type: string;
    market: 'spot' | 'on-demand';
    worker_name: string;
    state: 'starting' | 'running' | 'ended';
    launched_at: string;
    hours: number;
    end_reason: string | null;
}
interface Launcher {
    settings: Settings;
    hours_this_month: number;
    launches: Launch[];
}

async function send(method: string, path: string, body?: unknown) {
    const res = await fetch(`/api${path}`, {
        method,
        credentials: 'include',
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        const detail = data.detail;
        throw new Error(typeof detail === 'string' ? detail
            : Array.isArray(detail) ? detail.map((d: { msg?: string; loc?: string[] }) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join('; ')
                : `${method} ${path} failed: ${res.status}`);
    }
    return data;
}

const input = 'w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-100';

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
    return (
        <label className="block text-xs text-zinc-400 space-y-1">
            <span>{label}</span>
            {children}
            {hint && <span className="block text-[11px] text-zinc-500">{hint}</span>}
        </label>
    );
}

/** Admins: start compute workers on demand when compute jobs wait with no worker free. */
export function OnDemandWorkers() {
    const queryClient = useQueryClient();
    const { data, isError, error } = useQuery<Launcher>({
        queryKey: ['compute-launcher'],
        queryFn: () => send('GET', '/compute-launcher'),
        refetchInterval: 30_000,
    });
    const [form, setForm] = useState<Settings | null>(null);
    const [groups, setGroups] = useState('');
    const [secret, setSecret] = useState('');
    const [checked, setChecked] = useState<string | null>(null);
    useEffect(() => {
        if (data && !form) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- the form starts from the saved settings once
            setForm({
                ...data.settings,
                server_url: data.settings.server_url || (typeof window === 'undefined' ? '' : `${window.location.origin}/api`),
            });
            setGroups(data.settings.security_group_ids.join(', '));
        }
    }, [data, form]);
    const save = useMutation({
        mutationFn: () => send('PUT', '/compute-launcher', {
            ...form,
            subnet_id: form?.subnet_id || null,
            image: form?.image || null,
            access_key_id: form?.access_key_id || null,
            security_group_ids: groups.split(',').map(group => group.trim()).filter(Boolean),
            secret_access_key: secret ? secret : null,
        }),
        onSuccess: () => {
            setSecret('');
            setChecked(null);
            queryClient.invalidateQueries({ queryKey: ['compute-launcher'] });
        },
    });
    const check = useMutation({
        mutationFn: () => send('POST', '/compute-launcher/check'),
        onSuccess: result => setChecked(result.message),
        onError: () => setChecked(null),
    });

    if (isError) return <p className="text-red-300 text-sm">Could not load the on-demand settings: {(error as Error).message}</p>;
    if (!data || !form) return null;
    const set = (patch: Partial<Settings>) => setForm({ ...form, ...patch });
    const number = (value: string) => (value === '' ? 0 : Number(value));

    return (
        <section data-testid="on-demand-workers" className="mb-6 p-4 border border-zinc-800 rounded-xl bg-zinc-900/50 space-y-4">
            <div>
                <h2 className="font-semibold text-white">On-demand workers</h2>
                <p className="text-sm text-zinc-400 mt-1">
                    When a compute job, such as a map re-fit, has waited with no worker free to take it, start an AWS spot
                    machine that runs the worker and powers off once it has been idle (on-demand only when AWS has no spot
                    capacity). For this, the server&apos;s job runner must leave compute jobs to workers:{' '}
                    <code className="text-zinc-300">JOB_RUNNER_PLACEMENTS=server</code>. The machines&apos; AWS setup is in
                    docs/ON_DEMAND_WORKERS.md.
                </p>
            </div>
            <label className="flex items-center gap-2 text-sm text-zinc-200">
                <input type="checkbox" aria-label="Start workers on demand" checked={form.enabled}
                    onChange={e => set({ enabled: e.target.checked })} />
                Start workers on demand
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <Field label="AWS region"><input aria-label="AWS region" className={input} value={form.region}
                    placeholder="us-east-1" onChange={e => set({ region: e.target.value.trim() })} /></Field>
                <Field label="Instance type" hint="x86_64; its memory bounds the biggest fit">
                    <input aria-label="Instance type" className={input} value={form.instance_type}
                        onChange={e => set({ instance_type: e.target.value.trim() })} /></Field>
                <Field label="Server URL" hint="The API address the machines reach this server at">
                    <input aria-label="Server URL" className={input} value={form.server_url}
                        onChange={e => set({ server_url: e.target.value.trim() })} /></Field>
                <Field label="Image" hint="Empty: this version's ghcr.io/freedomridge/backend">
                    <input aria-label="Image" className={input} value={form.image ?? ''}
                        onChange={e => set({ image: e.target.value.trim() || null })} /></Field>
                <Field label="Subnet" hint="Empty: the region's default VPC">
                    <input aria-label="Subnet" className={input} value={form.subnet_id ?? ''} placeholder="subnet-…"
                        onChange={e => set({ subnet_id: e.target.value.trim() || null })} /></Field>
                <Field label="Security groups" hint="Comma separated; empty: the VPC's default">
                    <input aria-label="Security groups" className={input} value={groups} placeholder="sg-…"
                        onChange={e => setGroups(e.target.value)} /></Field>
                <Field label="Access key ID" hint="Empty: the server's own AWS credentials (its role)">
                    <input aria-label="Access key ID" className={input} value={form.access_key_id ?? ''}
                        onChange={e => set({ access_key_id: e.target.value.trim() || null })} /></Field>
                <Field label="Secret access key" hint={form.has_secret ? 'Saved; type a new one to replace it' : undefined}>
                    <input aria-label="Secret access key" type="password" autoComplete="off" className={input} value={secret}
                        onChange={e => setSecret(e.target.value)} /></Field>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
                <Field label="Wait (minutes)"><input aria-label="Wait minutes" type="number" min={1} className={input}
                    value={form.wait_minutes} onChange={e => set({ wait_minutes: number(e.target.value) })} /></Field>
                <Field label="Idle exit (minutes)"><input aria-label="Idle minutes" type="number" min={1} className={input}
                    value={form.idle_minutes} onChange={e => set({ idle_minutes: number(e.target.value) })} /></Field>
                <Field label="Machines at once"><input aria-label="Machines at once" type="number" min={1} className={input}
                    value={form.max_machines} onChange={e => set({ max_machines: number(e.target.value) })} /></Field>
                <Field label="Hours per machine"><input aria-label="Hours per machine" type="number" min={0.5} step={0.5}
                    className={input} value={form.max_hours_per_machine}
                    onChange={e => set({ max_hours_per_machine: number(e.target.value) })} /></Field>
                <Field label="Machine-hours a month"><input aria-label="Machine-hours a month" type="number" min={1}
                    className={input} value={form.monthly_hours}
                    onChange={e => set({ monthly_hours: number(e.target.value) })} /></Field>
            </div>
            <div className="flex flex-wrap items-center gap-3 text-sm">
                <button onClick={() => save.mutate()} disabled={save.isPending}
                    className="px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white">
                    {save.isPending ? 'Saving…' : 'Save'}
                </button>
                <button onClick={() => check.mutate()} disabled={check.isPending || !data.settings.region}
                    title="Ask AWS whether it would start a machine with the saved settings (nothing starts)"
                    className="px-3 py-1.5 rounded border border-zinc-700 text-zinc-200 hover:bg-zinc-800 disabled:opacity-40">
                    {check.isPending ? 'Asking AWS…' : 'Check with AWS'}
                </button>
                <span data-testid="on-demand-hours" className="text-zinc-400">
                    {data.hours_this_month.toFixed(1)} of {data.settings.monthly_hours} machine-hours used this month
                </span>
            </div>
            {save.error && <p className="text-sm text-red-300">{(save.error as Error).message}</p>}
            {check.error && <p className="text-sm text-red-300">{(check.error as Error).message}</p>}
            {checked && <p className="text-sm text-green-400">{checked}</p>}
            {data.settings.last_error && (
                <p data-testid="on-demand-error" className="text-sm text-red-300">
                    {data.settings.last_error_at && `${new Date(data.settings.last_error_at).toLocaleString()}: `}
                    {data.settings.last_error}
                </p>
            )}
            {data.launches.length > 0 && (
                <table className="w-full text-xs text-zinc-300">
                    <thead className="text-zinc-500 text-left">
                        <tr><th className="py-1">Machine</th><th>Type</th><th>State</th><th>Started</th><th>Hours</th><th>Ended because</th></tr>
                    </thead>
                    <tbody>
                        {data.launches.map(launch => (
                            <tr key={launch.id} data-testid="on-demand-launch" className="border-t border-zinc-800">
                                <td className="py-1 font-mono">{launch.instance_id ?? launch.worker_name}</td>
                                <td>{launch.instance_type}, {launch.market}</td>
                                <td>{launch.state}</td>
                                <td>{new Date(launch.launched_at).toLocaleString()}</td>
                                <td>{Number(launch.hours).toFixed(2)}</td>
                                <td className="text-zinc-400">{launch.end_reason ?? ''}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </section>
    );
}
