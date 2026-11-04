'use client';

import { useState } from 'react';
import { Header } from '@/components/header';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { useUser } from '@/contexts/UserContext';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
    getStorageConfigs,
    createStorageConfig,
    updateStorageConfig,
    deleteStorageConfig,
    testStorageConnection,
    getDatasets,
    StorageConfig,
    StorageConfigCreate,
    TestConnectionResult
} from '@/lib/api';
import { ArrowLeft, Plus, Cloud, Trash2, Edit2, Check, X, Loader2, AlertCircle, CheckCircle } from 'lucide-react';
import Link from 'next/link';

interface StorageFormData {
    name: string;
    bucket_name: string;
    region: string;
    access_key_id: string;
    secret_access_key: string;
    endpoint_url: string;
    is_public_bucket: boolean;
    is_default: boolean;
    // Comma-separated glob patterns typed into the form, e.g. "client-*, logs-2025-??".
    // Split/trimmed into an array before hitting the API.
    bucket_patterns: string;
    // false: every dataset can use this config. true: only dataset_ids.
    restricted: boolean;
    dataset_ids: number[];
}

const initialFormData: StorageFormData = {
    name: '',
    bucket_name: '',
    region: 'us-east-1',
    access_key_id: '',
    secret_access_key: '',
    endpoint_url: '',
    is_public_bucket: false,
    is_default: false,
    bucket_patterns: '',
    restricted: false,
    dataset_ids: [],
};

export default function StorageSettingsPage() {
    const { user } = useUser();
    const queryClient = useQueryClient();
    const [showForm, setShowForm] = useState(false);
    const [editingId, setEditingId] = useState<number | null>(null);
    const [formData, setFormData] = useState<StorageFormData>(initialFormData);
    const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);
    const [testingId, setTestingId] = useState<number | null>(null);
    const [datasetFilter, setDatasetFilter] = useState('');

    // Check if user is admin
    const isAdmin = user?.role === 'admin';

    // Fetch storage configs
    const { data: configs, isLoading, error } = useQuery({
        queryKey: ['storage-configs'],
        queryFn: getStorageConfigs,
        enabled: isAdmin,
    });

    // Datasets, to pick which ones a limited config serves and to name them on each config
    const { data: datasets, error: datasetsError } = useQuery({
        queryKey: ['datasets'],
        queryFn: getDatasets,
        enabled: isAdmin,
    });
    const datasetNames = new Map((datasets ?? []).map(d => [d.id, d.name]));
    const filteredDatasets = (datasets ?? []).filter(d =>
        d.name.toLowerCase().includes(datasetFilter.trim().toLowerCase()));
    const toggleDataset = (id: number) => setFormData({
        ...formData,
        dataset_ids: formData.dataset_ids.includes(id)
            ? formData.dataset_ids.filter(existing => existing !== id)
            : [...formData.dataset_ids, id],
    });

    // Create mutation
    const createMutation = useMutation({
        mutationFn: createStorageConfig,
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['storage-configs'] });
            setShowForm(false);
            setFormData(initialFormData);
        },
    });

    // Update mutation
    const updateMutation = useMutation({
        mutationFn: ({ id, data }: { id: number; data: Partial<StorageConfigCreate> }) =>
            updateStorageConfig(id, data),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['storage-configs'] });
            setEditingId(null);
            setFormData(initialFormData);
        },
    });

    // Delete mutation
    const deleteMutation = useMutation({
        mutationFn: deleteStorageConfig,
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['storage-configs'] });
        },
    });

    // Test connection mutation
    const testMutation = useMutation({
        mutationFn: testStorageConnection,
        onSuccess: (result) => {
            setTestResult(result);
        },
        onError: (error: Error) => {
            setTestResult({ success: false, message: error.message });
        },
    });

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        const patterns = formData.bucket_patterns
            .split(',')
            .map(p => p.trim())
            .filter(Boolean);

        const data: StorageConfigCreate = {
            name: formData.name,
            bucket_name: formData.bucket_name,
            region: formData.region,
            is_public_bucket: formData.is_public_bucket,
            is_default: formData.is_default,
            bucket_patterns: patterns,
            restricted: formData.restricted,
            dataset_ids: formData.restricted ? formData.dataset_ids : [],
        };

        if (!formData.is_public_bucket) {
            if (formData.access_key_id) data.access_key_id = formData.access_key_id;
            if (formData.secret_access_key) data.secret_access_key = formData.secret_access_key;
        }
        if (formData.endpoint_url) data.endpoint_url = formData.endpoint_url;

        if (editingId) {
            updateMutation.mutate({ id: editingId, data });
        } else {
            createMutation.mutate(data);
        }
    };

    const handleEdit = (config: StorageConfig) => {
        setEditingId(config.id);
        setFormData({
            name: config.name,
            bucket_name: config.bucket_name,
            region: config.region,
            access_key_id: '',  // Don't populate for security
            secret_access_key: '',
            endpoint_url: config.endpoint_url || '',
            is_public_bucket: config.is_public_bucket,
            is_default: config.is_default,
            bucket_patterns: (config.bucket_patterns || []).join(', '),
            restricted: config.restricted,
            dataset_ids: config.dataset_ids || [],
        });
        setShowForm(true);
    };

    const handleTest = (id: number) => {
        setTestingId(id);
        setTestResult(null);
        testMutation.mutate(id);
    };

    const handleCancel = () => {
        setShowForm(false);
        setEditingId(null);
        setFormData(initialFormData);
        setTestResult(null);
        setDatasetFilter('');
    };

    if (!isAdmin) {
        return (
            <ProtectedRoute>
                <div className="min-h-screen bg-zinc-950 flex flex-col">
                    <Header />
                    <main className="flex-1 container mx-auto px-6 py-8">
                        <div className="text-center py-12">
                            <AlertCircle className="mx-auto h-12 w-12 text-red-400" />
                            <h2 className="mt-4 text-xl font-semibold text-white">Access Denied</h2>
                            <p className="mt-2 text-zinc-400">Only administrators can manage storage configurations.</p>
                        </div>
                    </main>
                </div>
            </ProtectedRoute>
        );
    }

    return (
        <ProtectedRoute>
            <div className="min-h-screen bg-zinc-950 flex flex-col">
                <Header />

                <main className="flex-1 container mx-auto px-6 py-8">
                    {/* Header */}
                    <div className="flex items-center gap-4 mb-8">
                        <Link
                            href="/settings"
                            className="p-2 hover:bg-zinc-800 rounded-lg transition-colors"
                        >
                            <ArrowLeft className="w-5 h-5 text-zinc-400" />
                        </Link>
                        <div>
                            <h1 className="text-3xl font-bold text-white">Storage Configuration</h1>
                            <p className="text-zinc-400 mt-1">Configure S3 buckets for remote file storage</p>
                        </div>
                    </div>

                    {/* Add Storage Button */}
                    {!showForm && (
                        <button
                            onClick={() => setShowForm(true)}
                            className="mb-6 flex items-center gap-2 px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg transition-colors"
                        >
                            <Plus className="w-4 h-4" />
                            Add S3 Bucket
                        </button>
                    )}

                    {/* Add/Edit Form */}
                    {showForm && (
                        <div className="mb-8 p-6 bg-zinc-900/50 border border-zinc-800 rounded-xl">
                            <h2 className="text-xl font-semibold text-white mb-4">
                                {editingId ? 'Edit Storage Configuration' : 'Add S3 Bucket'}
                            </h2>
                            <form onSubmit={handleSubmit} className="space-y-4">
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <div>
                                        <label className="block text-sm font-medium text-zinc-300 mb-1">
                                            Name *
                                        </label>
                                        <input
                                            type="text"
                                            required
                                            value={formData.name}
                                            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                                            placeholder="My S3 Bucket"
                                            className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-zinc-300 mb-1">
                                            Bucket Name *
                                        </label>
                                        <input
                                            type="text"
                                            required
                                            value={formData.bucket_name}
                                            onChange={(e) => setFormData({ ...formData, bucket_name: e.target.value })}
                                            placeholder="my-bucket-name"
                                            className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-zinc-300 mb-1">
                                            Region
                                        </label>
                                        <input
                                            type="text"
                                            value={formData.region}
                                            onChange={(e) => setFormData({ ...formData, region: e.target.value })}
                                            placeholder="us-east-1"
                                            className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-zinc-300 mb-1">
                                            Endpoint URL (optional, for MinIO)
                                        </label>
                                        <input
                                            type="text"
                                            value={formData.endpoint_url}
                                            onChange={(e) => setFormData({ ...formData, endpoint_url: e.target.value })}
                                            placeholder="https://minio.example.com"
                                            className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                        />
                                    </div>
                                </div>

                                {/* Additional bucket patterns */}
                                <div>
                                    <label className="block text-sm font-medium text-zinc-300 mb-1">
                                        Additional bucket patterns (optional)
                                    </label>
                                    <input
                                        type="text"
                                        value={formData.bucket_patterns}
                                        onChange={(e) => setFormData({ ...formData, bucket_patterns: e.target.value })}
                                        placeholder="client-*, logs-2025-??"
                                        className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                    />
                                    <p className="text-xs text-zinc-500 mt-1">
                                        Comma-separated glob patterns for other buckets these credentials can access.
                                        Use <code className="font-mono text-zinc-400">*</code> to match any characters,
                                        <code className="font-mono text-zinc-400"> ?</code> for one. When several configs
                                        cover a bucket, a dataset uses its own config first, then one limited to it, then
                                        one serving all datasets; among those, an exact
                                        <code className="font-mono text-zinc-400"> Bucket Name</code> match wins over patterns.
                                    </p>
                                </div>

                                {/* Public bucket toggle */}
                                <div className="flex items-center gap-3">
                                    <input
                                        type="checkbox"
                                        id="is_public_bucket"
                                        checked={formData.is_public_bucket}
                                        onChange={(e) => setFormData({ ...formData, is_public_bucket: e.target.checked })}
                                        className="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-cyan-500 focus:ring-cyan-500"
                                    />
                                    <label htmlFor="is_public_bucket" className="text-sm text-zinc-300">
                                        Public bucket (no credentials required)
                                    </label>
                                </div>

                                {/* Credentials (only if not public) */}
                                {!formData.is_public_bucket && (
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-4 bg-zinc-800/50 rounded-lg border border-zinc-700">
                                        <div>
                                            <label className="block text-sm font-medium text-zinc-300 mb-1">
                                                Access Key ID
                                            </label>
                                            <input
                                                type="text"
                                                value={formData.access_key_id}
                                                onChange={(e) => setFormData({ ...formData, access_key_id: e.target.value })}
                                                placeholder="AKIA..."
                                                className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                            />
                                        </div>
                                        <div>
                                            <label className="block text-sm font-medium text-zinc-300 mb-1">
                                                Secret Access Key
                                            </label>
                                            <input
                                                type="password"
                                                value={formData.secret_access_key}
                                                onChange={(e) => setFormData({ ...formData, secret_access_key: e.target.value })}
                                                placeholder={editingId ? '(unchanged)' : 'Enter secret key'}
                                                className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                            />
                                        </div>
                                    </div>
                                )}

                                {/* Which datasets may use these credentials */}
                                <fieldset data-testid="storage-dataset-scope">
                                    <legend className="block text-sm font-medium text-zinc-300 mb-2">Datasets</legend>
                                    <div className="flex flex-wrap gap-4">
                                        <label className="flex items-center gap-2 text-sm text-zinc-300">
                                            <input
                                                type="radio"
                                                name="dataset_scope"
                                                checked={!formData.restricted}
                                                onChange={() => setFormData({ ...formData, restricted: false })}
                                                className="w-4 h-4 border-zinc-600 bg-zinc-800 text-cyan-500 focus:ring-cyan-500"
                                            />
                                            All datasets
                                        </label>
                                        <label className={`flex items-center gap-2 text-sm ${formData.is_default ? 'text-zinc-500' : 'text-zinc-300'}`}>
                                            <input
                                                type="radio"
                                                name="dataset_scope"
                                                checked={formData.restricted}
                                                disabled={formData.is_default}
                                                onChange={() => setFormData({ ...formData, restricted: true })}
                                                className="w-4 h-4 border-zinc-600 bg-zinc-800 text-cyan-500 focus:ring-cyan-500"
                                            />
                                            Only these datasets
                                        </label>
                                    </div>
                                    <p className="text-xs text-zinc-500 mt-1">
                                        {formData.is_default
                                            ? "The default storage replaces the server's environment credentials for every dataset, so it can't be limited."
                                            : formData.restricted
                                                ? 'Only the datasets checked below can load media through these credentials.'
                                                : 'Every dataset can load media from these buckets through these credentials.'}
                                    </p>
                                    {formData.restricted && (
                                        <div className="mt-2 p-3 bg-zinc-800/50 rounded-lg border border-zinc-700">
                                            <input
                                                type="text"
                                                value={datasetFilter}
                                                onChange={(e) => setDatasetFilter(e.target.value)}
                                                placeholder="Filter datasets"
                                                className="w-full mb-2 px-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-cyan-500"
                                            />
                                            <div className="max-h-48 overflow-y-auto space-y-1">
                                                {datasetsError ? (
                                                    <p className="text-sm text-red-400">
                                                        Couldn&apos;t load datasets: {(datasetsError as Error).message}
                                                    </p>
                                                ) : !datasets ? (
                                                    <p className="text-sm text-zinc-500">Loading datasets…</p>
                                                ) : filteredDatasets.length === 0 ? (
                                                    <p className="text-sm text-zinc-500">No datasets match.</p>
                                                ) : filteredDatasets.map(d => (
                                                    <label key={d.id} className="flex items-center gap-2 text-sm text-zinc-300">
                                                        <input
                                                            type="checkbox"
                                                            checked={formData.dataset_ids.includes(d.id)}
                                                            onChange={() => toggleDataset(d.id)}
                                                            className="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-cyan-500 focus:ring-cyan-500"
                                                        />
                                                        {d.name}
                                                    </label>
                                                ))}
                                            </div>
                                            {formData.dataset_ids.length === 0 && (
                                                <p className="text-xs text-amber-400 mt-2">
                                                    No datasets selected: these credentials won&apos;t serve any dataset.
                                                </p>
                                            )}
                                        </div>
                                    )}
                                </fieldset>

                                {/* Default toggle */}
                                <div className="flex items-center gap-3">
                                    <input
                                        type="checkbox"
                                        id="is_default"
                                        checked={formData.is_default}
                                        disabled={formData.restricted}
                                        onChange={(e) => setFormData({ ...formData, is_default: e.target.checked })}
                                        className="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-cyan-500 focus:ring-cyan-500"
                                    />
                                    <label htmlFor="is_default" className={`text-sm ${formData.restricted ? 'text-zinc-500' : 'text-zinc-300'}`}>
                                        Set as default storage
                                        {formData.restricted && ' (only a config serving all datasets can be the default)'}
                                    </label>
                                </div>

                                {/* Form actions */}
                                <div className="flex gap-3 pt-2">
                                    <button
                                        type="submit"
                                        disabled={createMutation.isPending || updateMutation.isPending}
                                        className="flex items-center gap-2 px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white rounded-lg transition-colors"
                                    >
                                        {(createMutation.isPending || updateMutation.isPending) ? (
                                            <Loader2 className="w-4 h-4 animate-spin" />
                                        ) : (
                                            <Check className="w-4 h-4" />
                                        )}
                                        {editingId ? 'Update' : 'Create'}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={handleCancel}
                                        className="flex items-center gap-2 px-4 py-2 bg-zinc-700 hover:bg-zinc-600 text-white rounded-lg transition-colors"
                                    >
                                        <X className="w-4 h-4" />
                                        Cancel
                                    </button>
                                </div>

                                {/* Error display */}
                                {(createMutation.error || updateMutation.error) && (
                                    <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                                        {(createMutation.error as Error)?.message || (updateMutation.error as Error)?.message}
                                    </div>
                                )}
                            </form>
                        </div>
                    )}

                    {/* Loading state */}
                    {isLoading && (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="w-8 h-8 animate-spin text-cyan-400" />
                        </div>
                    )}

                    {/* Error state */}
                    {error && (
                        <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg">
                            <p className="text-red-400">{(error as Error).message}</p>
                        </div>
                    )}

                    {/* Delete error */}
                    {deleteMutation.error && (
                        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                            {(deleteMutation.error as Error).message}
                        </div>
                    )}

                    {/* Storage configs list */}
                    {configs && configs.length > 0 && (
                        <div className="space-y-4">
                            {configs.map((config) => (
                                <div
                                    key={config.id}
                                    className="p-4 bg-zinc-900/50 border border-zinc-800 rounded-xl hover:border-zinc-700 transition-colors"
                                >
                                    <div className="flex items-start justify-between">
                                        <div className="flex items-start gap-4">
                                            <div className="p-3 bg-cyan-500/10 rounded-lg text-cyan-400">
                                                <Cloud className="w-6 h-6" />
                                            </div>
                                            <div>
                                                <div className="flex items-center gap-2">
                                                    <h3 className="text-lg font-semibold text-white">{config.name}</h3>
                                                    {config.is_default && (
                                                        <span className="px-2 py-0.5 text-xs bg-cyan-500/20 text-cyan-400 rounded">
                                                            Default
                                                        </span>
                                                    )}
                                                    {config.is_public_bucket && (
                                                        <span className="px-2 py-0.5 text-xs bg-green-500/20 text-green-400 rounded">
                                                            Public
                                                        </span>
                                                    )}
                                                </div>
                                                <p className="text-zinc-400 text-sm mt-1">
                                                    <span className="font-mono">{config.bucket_name}</span>
                                                    <span className="mx-2">•</span>
                                                    {config.region}
                                                    {config.endpoint_url && (
                                                        <>
                                                            <span className="mx-2">•</span>
                                                            <span className="text-zinc-500">{config.endpoint_url}</span>
                                                        </>
                                                    )}
                                                </p>
                                                {config.bucket_patterns && config.bucket_patterns.length > 0 && (
                                                    <div className="flex flex-wrap gap-1 mt-2">
                                                        {config.bucket_patterns.map((pat) => (
                                                            <span
                                                                key={pat}
                                                                className="px-2 py-0.5 text-xs bg-zinc-800 text-zinc-300 rounded font-mono"
                                                            >
                                                                {pat}
                                                            </span>
                                                        ))}
                                                    </div>
                                                )}
                                                <p className="text-zinc-500 text-xs mt-1" data-testid="storage-config-datasets">
                                                    Datasets: {!config.restricted
                                                        ? 'all'
                                                        : config.dataset_ids.length === 0
                                                            ? 'none (serves no dataset)'
                                                            : config.dataset_ids.map(id => datasetNames.get(id) ?? `#${id}`).join(', ')}
                                                </p>
                                                {config.access_key_id && (
                                                    <p className="text-zinc-500 text-xs mt-1">
                                                        Access Key: {config.access_key_id}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <button
                                                onClick={() => handleTest(config.id)}
                                                disabled={testMutation.isPending && testingId === config.id}
                                                className="px-3 py-1.5 text-sm bg-zinc-700 hover:bg-zinc-600 disabled:opacity-50 text-white rounded-lg transition-colors"
                                            >
                                                {testMutation.isPending && testingId === config.id ? (
                                                    <Loader2 className="w-4 h-4 animate-spin" />
                                                ) : (
                                                    'Test Connection'
                                                )}
                                            </button>
                                            <button
                                                onClick={() => handleEdit(config)}
                                                aria-label={`Edit ${config.name}`}
                                                className="p-2 hover:bg-zinc-700 rounded-lg transition-colors text-zinc-400 hover:text-white"
                                            >
                                                <Edit2 className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => {
                                                    if (confirm(`Delete storage configuration "${config.name}"?`)) {
                                                        deleteMutation.mutate(config.id);
                                                    }
                                                }}
                                                disabled={deleteMutation.isPending}
                                                className="p-2 hover:bg-red-500/20 rounded-lg transition-colors text-zinc-400 hover:text-red-400"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </div>

                                    {/* Test result display */}
                                    {testResult && testingId === config.id && (
                                        <div className={`mt-4 p-3 rounded-lg ${testResult.success
                                                ? 'bg-green-500/10 border border-green-500/30'
                                                : 'bg-red-500/10 border border-red-500/30'
                                            }`}>
                                            <div className="flex items-center gap-2">
                                                {testResult.success ? (
                                                    <CheckCircle className="w-5 h-5 text-green-400" />
                                                ) : (
                                                    <AlertCircle className="w-5 h-5 text-red-400" />
                                                )}
                                                <span className={testResult.success ? 'text-green-400' : 'text-red-400'}>
                                                    {testResult.message}
                                                </span>
                                            </div>
                                            {testResult.bucket_location && (
                                                <p className="text-zinc-400 text-sm mt-2">
                                                    Location: {testResult.bucket_location}
                                                </p>
                                            )}
                                            {testResult.sample_objects && testResult.sample_objects.length > 0 && (
                                                <div className="mt-2">
                                                    <p className="text-zinc-400 text-sm">Sample objects:</p>
                                                    <ul className="text-zinc-500 text-xs font-mono mt-1">
                                                        {testResult.sample_objects.map((obj, i) => (
                                                            <li key={i} className="truncate">{obj}</li>
                                                        ))}
                                                    </ul>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}

                    {/* Empty state */}
                    {configs && configs.length === 0 && !showForm && (
                        <div className="text-center py-12">
                            <Cloud className="mx-auto h-12 w-12 text-zinc-600" />
                            <h3 className="mt-4 text-lg font-medium text-white">No storage backends configured</h3>
                            <p className="mt-2 text-zinc-400">
                                Add an S3 bucket to enable remote file storage.
                            </p>
                        </div>
                    )}
                </main>
            </div>
        </ProtectedRoute>
    );
}
