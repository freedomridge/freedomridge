'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Copy, Trash2, Plus, Check } from 'lucide-react';
import Link from 'next/link';
import { Header } from '@/components/header';
import { PermissionGate } from '@/components/PermissionGate';

interface Token {
    id: number;
    name: string;
    token?: string;  // Only present on creation
    created_at: string;
    last_used_at?: string;
    expires_at?: string;
    is_active: boolean;
}

export default function TokensPage() {
    const [showCreateForm, setShowCreateForm] = useState(false);
    const [newTokenName, setNewTokenName] = useState('');
    const [expiryDays, setExpiryDays] = useState(90);
    const [createdToken, setCreatedToken] = useState<Token | null>(null);
    const [copiedToken, setCopiedToken] = useState(false);

    const queryClient = useQueryClient();

    // Fetch tokens
    const { data: tokens, isLoading } = useQuery<Token[]>({
        queryKey: ['api-tokens'],
        queryFn: async () => {
            const res = await fetch('/api/tokens', { credentials: 'include' });
            if (!res.ok) throw new Error('Failed to fetch tokens');
            return res.json();
        }
    });

    // Create token
    const createMutation = useMutation({
        mutationFn: async (data: { name: string, expires_days: number }) => {
            const res = await fetch('/api/tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(data)
            });
            if (!res.ok) throw new Error('Failed to create token');
            return res.json();
        },
        onSuccess: (data) => {
            setCreatedToken(data);
            setShowCreateForm(false);
            setNewTokenName('');
            queryClient.invalidateQueries({ queryKey: ['api-tokens'] });
        }
    });

    // Revoke token
    const revokeMutation = useMutation({
        mutationFn: async (id: number) => {
            const res = await fetch(`/api/tokens/${id}`, {
                method: 'DELETE',
                credentials: 'include'
            });
            if (!res.ok) throw new Error('Failed to revoke token');
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['api-tokens'] });
        }
    });

    const copyToken = async (token: string) => {
        await navigator.clipboard.writeText(token);
        setCopiedToken(true);
        setTimeout(() => setCopiedToken(false), 2000);
    };


    return (
        <div className="min-h-screen bg-zinc-950 text-white">
            <Header />

            <div className="p-8 max-w-4xl mx-auto">
                <div className="mb-6">
                    <Link href="/settings" className="text-zinc-400 hover:text-white transition-colors text-sm mb-4 inline-block">
                        ← Back to Settings
                    </Link>
                    <h1 className="text-3xl font-bold mb-2">API Tokens</h1>
                    <p className="text-zinc-400">
                        Create tokens for authenticating scripts and SDK usage. Tokens inherit your role permissions.
                    </p>
                </div>

                {/* Created Token Success Banner */}
                {createdToken && (
                    <div className="mb-6 p-4 bg-green-900/20 border border-green-600 rounded-lg">
                        <div className="flex items-start justify-between mb-2">
                            <div>
                                <h3 className="font-semibold text-green-400 mb-1">Token Created!</h3>
                                <p className="text-sm text-zinc-300 mb-3">
                                    Copy this token now - you won&apos;t be able to see it again:
                                </p>
                            </div>
                            <button
                                onClick={() => setCreatedToken(null)}
                                className="text-zinc-400 hover:text-white"
                            >
                                ×
                            </button>
                        </div>
                        <div className="flex gap-2">
                            <code className="flex-1 p-3 bg-zinc-900 border border-zinc-700 rounded font-mono text-sm overflow-x-auto">
                                {createdToken.token}
                            </code>
                            <button
                                onClick={() => copyToken(createdToken.token!)}
                                className="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 text-white rounded flex items-center gap-2"
                                title="Copy token"
                            >
                                {copiedToken ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                            </button>
                        </div>
                        <p className="mt-3 text-xs text-zinc-400">
                            Usage: <code className="bg-zinc-800 px-2 py-1 rounded">Authorization: Bearer {'{'}token{'}'}</code>
                        </p>
                    </div>
                )}

                {/* Create Form - Only for power_user and admin */}
                <PermissionGate
                    role="power_user"
                    fallback={
                        <div className="mb-6 p-4 border border-zinc-700 rounded-lg bg-zinc-900/50">
                            <p className="text-zinc-400 text-sm">
                                Guest users cannot create API tokens. Contact an administrator to upgrade your account.
                            </p>
                        </div>
                    }
                >
                    {showCreateForm ? (
                        <div className="mb-6 p-4 border border-zinc-700 rounded-lg bg-zinc-900">
                            <h3 className="font-semibold mb-4">Create New Token</h3>
                            <div className="space-y-3">
                                <div>
                                    <label className="block text-sm font-medium mb-1 text-zinc-300">
                                        Token Name
                                    </label>
                                    <input
                                        type="text"
                                        placeholder="e.g., 'my-laptop', 'ci-server', 'data-pipeline'"
                                        value={newTokenName}
                                        onChange={(e) => setNewTokenName(e.target.value)}
                                        className="w-full p-2 bg-zinc-800 border border-zinc-700 rounded text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium mb-1 text-zinc-300">
                                        Expiration
                                    </label>
                                    <select
                                        value={expiryDays}
                                        onChange={(e) => setExpiryDays(Number(e.target.value))}
                                        className="w-full p-2 bg-zinc-800 border border-zinc-700 rounded text-white focus:outline-none focus:border-cyan-500"
                                    >
                                        <option value={30}>30 days</option>
                                        <option value={90}>90 days (recommended)</option>
                                        <option value={180}>180 days</option>
                                        <option value={365}>1 year</option>
                                    </select>
                                </div>
                                <div className="flex gap-2 pt-2">
                                    <button
                                        onClick={() => createMutation.mutate({ name: newTokenName, expires_days: expiryDays })}
                                        disabled={!newTokenName.trim() || createMutation.isPending}
                                        className="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:bg-zinc-700 disabled:cursor-not-allowed text-white rounded font-medium"
                                    >
                                        {createMutation.isPending ? 'Creating...' : 'Create Token'}
                                    </button>
                                    <button
                                        onClick={() => {
                                            setShowCreateForm(false);
                                            setNewTokenName('');
                                        }}
                                        className="px-4 py-2 border border-zinc-700 hover:bg-zinc-800 rounded"
                                        disabled={createMutation.isPending}
                                    >
                                        Cancel
                                    </button>
                                </div>
                            </div>
                        </div>
                    ) : (
                        <button
                            onClick={() => setShowCreateForm(true)}
                            className="mb-6 px-4 py-2 bg-cyan-600 hover:bg-cyan-700 text-white rounded font-medium flex items-center gap-2"
                        >
                            <Plus className="w-4 h-4" />
                            Create New Token
                        </button>
                    )}
                </PermissionGate>

                {/* Token List */}
                <div className="space-y-3">
                    {isLoading ? (
                        <div className="text-center py-8 text-zinc-500">
                            Loading tokens...
                        </div>
                    ) : tokens && tokens.length > 0 ? (
                        tokens.map(token => (
                            <div key={token.id} className="p-4 border border-zinc-700 rounded-lg bg-zinc-900 flex justify-between items-start">
                                <div className="flex-1">
                                    <h3 className="font-semibold text-lg">{token.name}</h3>
                                    <div className="mt-2 space-y-1 text-sm text-zinc-400">
                                        <p>
                                            <span className="text-zinc-500">Created:</span>{' '}
                                            {new Date(token.created_at).toLocaleDateString('en-US', {
                                                year: 'numeric',
                                                month: 'long',
                                                day: 'numeric'
                                            })}
                                        </p>
                                        {token.last_used_at ? (
                                            <p>
                                                <span className="text-zinc-500">Last used:</span>{' '}
                                                {new Date(token.last_used_at).toLocaleDateString('en-US', {
                                                    year: 'numeric',
                                                    month: 'long',
                                                    day: 'numeric'
                                                })}
                                            </p>
                                        ) : (
                                            <p className="text-yellow-600">Never used</p>
                                        )}
                                        {token.expires_at && (
                                            <p>
                                                <span className="text-zinc-500">Expires:</span>{' '}
                                                {new Date(token.expires_at).toLocaleDateString('en-US', {
                                                    year: 'numeric',
                                                    month: 'long',
                                                    day: 'numeric'
                                                })}
                                            </p>
                                        )}
                                    </div>
                                </div>
                                <button
                                    onClick={() => {
                                        if (confirm(`Revoke token "${token.name}"? This cannot be undone.`)) {
                                            revokeMutation.mutate(token.id);
                                        }
                                    }}
                                    disabled={revokeMutation.isPending}
                                    className="px-3 py-2 text-red-400 hover:text-red-300 hover:bg-red-900/20 rounded flex items-center gap-2"
                                    title="Revoke token"
                                >
                                    <Trash2 className="w-4 h-4" />
                                    <span className="text-sm">Revoke</span>
                                </button>
                            </div>
                        ))
                    ) : (
                        <div className="text-center py-12 text-zinc-500">
                            <p className="mb-2">No API tokens yet</p>
                            <p className="text-sm">Create a token to authenticate scripts and SDK usage</p>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
