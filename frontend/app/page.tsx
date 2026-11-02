'use client';

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getDatasets, deleteDataset } from '@/lib/api';
import Link from 'next/link';
import { Trash2, X, Plus, Database } from 'lucide-react';
import { useState } from 'react';

import { ProtectedRoute } from '@/components/ProtectedRoute';
import { Header } from '@/components/header';
import { DatasetDeletion } from '@/components/dataset-deletion';
import { CreateDatasetModal } from '@/components/create-dataset-modal';
import { useUser } from '@/contexts/UserContext';

export default function Home() {
  const queryClient = useQueryClient();
  const { user } = useUser();
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: number; name: string } | null>(null);

  const { data: datasets, isLoading } = useQuery({
    queryKey: ['datasets'],
    queryFn: getDatasets,
    refetchInterval: query => query.state.data?.some(dataset => dataset.deleting) ? 5000 : false,
  });

  // Fetch latest version for each dataset
  const { data: versionMap } = useQuery({
    queryKey: ['dataset-versions-summary', datasets?.map(d => d.id).join(',')],
    queryFn: async () => {
      if (!datasets) return {};
      const map: Record<number, { name: string; created_at: string; sample_count: number } | null> = {};
      await Promise.all(datasets.map(async (ds) => {
        try {
          const res = await fetch(`/api/datasets/${ds.id}/versions`);
          if (res.ok) {
            const versions = await res.json();
            map[ds.id] = versions.length > 0 ? versions[0] : null; // newest first
          }
        } catch { map[ds.id] = null; }
      }));
      return map;
    },
    enabled: !!datasets && datasets.length > 0,
  });

  const deleteMutation = useMutation({
    mutationFn: deleteDataset,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['datasets'] });
      setDeletingId(null);
      setConfirmDelete(null);
    },
    onError: (error) => {
      console.error('Failed to delete dataset:', error);
      alert('Failed to delete dataset');
      setDeletingId(null);
      setConfirmDelete(null);
    }
  });

  const handleDeleteClick = (id: number, name: string, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setConfirmDelete({ id, name });
  };

  const handleConfirmDelete = () => {
    if (confirmDelete) {
      setDeletingId(confirmDelete.id);
      deleteMutation.mutate(confirmDelete.id);
    }
  };

  const handleCancelDelete = () => {
    setConfirmDelete(null);
  };

  return (
    <ProtectedRoute>
      <div className="min-h-screen bg-zinc-950 flex flex-col">
        <Header />

        <main className="flex-1 container mx-auto px-6 py-8">
          <div className="flex items-center justify-between mb-8">
            <div>
              <h1 className="text-3xl font-bold text-white">Datasets</h1>
              <p className="text-zinc-400 mt-1">Manage and explore your computer vision data</p>
            </div>
            {user?.role === 'admin' && <button onClick={() => setCreating(true)} className="inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 bg-cyan-600 text-white hover:bg-cyan-700 h-10 px-4 py-2 gap-2 shadow-lg shadow-cyan-900/20">
              <Plus size={16} />
              New Dataset
            </button>}
          </div>

          {isLoading ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-64 rounded-xl bg-zinc-900/50 border border-zinc-800 animate-pulse" />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
              {datasets?.map((dataset) => (
                <div key={dataset.id} className="group relative h-full">
                  <Link
                    href={`/datasets/${dataset.id}`}
                    className="block h-full bg-zinc-900/50 border border-zinc-800 rounded-xl overflow-hidden hover:border-cyan-500/50 hover:ring-1 hover:ring-cyan-500/50 transition-all duration-300 flex flex-col shadow-lg hover:shadow-cyan-900/10"
                  >
                    {/* Preview Area */}
                    <div className="h-48 bg-zinc-900 relative overflow-hidden group-hover:opacity-90 transition-opacity border-b border-zinc-800/50">
                      <div className="absolute inset-0 bg-gradient-to-br from-zinc-800 to-zinc-900 flex items-center justify-center">
                        <Database className="text-zinc-700 w-12 h-12" />
                      </div>
                      {/* Gradient Overlay */}
                      <div className="absolute inset-0 bg-gradient-to-t from-zinc-900/80 to-transparent" />

                      <div className="absolute bottom-3 left-4 right-4 flex justify-between items-end">
                        <span className="text-xs font-mono text-zinc-400 bg-black/40 backdrop-blur px-2 py-1 rounded border border-white/10">
                          ID: {dataset.id}
                        </span>
                      </div>
                    </div>

                    <div className="p-5 flex-1 flex flex-col">
                      <div className="flex justify-between items-start mb-2">
                        <h3 className="text-xl font-semibold text-white group-hover:text-cyan-400 transition-colors truncate pr-8">
                          {dataset.name}
                        </h3>
                      </div>

                      <p className="text-zinc-400 text-sm line-clamp-2 flex-1 mb-4">
                        {dataset.description || "No description provided."}
                      </p>

                      <div className="flex items-center justify-between text-xs text-zinc-500 font-mono border-t border-zinc-800/50 pt-4 mt-auto">
                        <div className="flex items-center gap-1">
                          {new Date(dataset.created_at).toLocaleDateString()}
                        </div>
                        {versionMap?.[dataset.id] && (
                          <span className="px-2 py-0.5 bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 rounded text-xs">
                            {versionMap[dataset.id]!.name}
                          </span>
                        )}
                      </div>
                    </div>
                  </Link>
                  {dataset.deleting && <DatasetDeletion dataset={dataset} />}

                  {!dataset.deleting && <button
                    type="button"
                    onClick={(e) => handleDeleteClick(dataset.id, dataset.name, e)}
                    disabled={deletingId === dataset.id}
                    className="absolute top-3 right-3 z-10 p-2 bg-black/40 hover:bg-red-500/20 text-zinc-400 hover:text-red-400 rounded-lg backdrop-blur opacity-0 group-hover:opacity-100 transition-all disabled:opacity-50 border border-white/5 hover:border-red-500/30"
                    title="Delete dataset"
                  >
                    {deletingId === dataset.id ? (
                      <div className="w-4 h-4 border-2 border-red-400 border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <Trash2 size={16} />
                    )}
                  </button>}
                </div>
              ))}

              {/* Empty State */}
              {datasets?.length === 0 && (
                <div className="col-span-full flex flex-col items-center justify-center py-20 text-center border-2 border-dashed border-zinc-800 rounded-xl bg-zinc-900/20">
                  <div className="bg-zinc-900 p-4 rounded-full mb-4">
                    <Database className="w-8 h-8 text-zinc-600" />
                  </div>
                  <h3 className="text-xl font-semibold text-white mb-2">No datasets yet</h3>
                  <p className="text-zinc-400 max-w-sm mb-6">
                    Get started by creating your first dataset or ingesting sample data.
                  </p>
                  {user?.role === 'admin' && <button onClick={() => setCreating(true)} className="inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 bg-cyan-600 text-white hover:bg-cyan-700 h-10 px-4 py-2 gap-2">
                    <Plus size={16} />
                    Create Dataset
                  </button>}
                </div>
              )}
            </div>
          )}
        </main>

        {creating && <CreateDatasetModal onClose={() => setCreating(false)} />}

        {/* Confirmation Modal */}
        {confirmDelete && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
            <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-6 max-w-md w-full shadow-2xl ring-1 ring-white/10">
              <div className="flex items-start justify-between mb-4">
                <h3 className="text-xl font-semibold text-white">Delete Dataset</h3>
                <button
                  onClick={handleCancelDelete}
                  className="p-1 hover:bg-zinc-800 rounded transition-colors text-zinc-400 hover:text-white"
                >
                  <X size={20} />
                </button>
              </div>

              <p className="text-zinc-300 mb-6 text-sm leading-relaxed">
                Are you sure you want to delete <span className="font-semibold text-white">&quot;{confirmDelete.name}&quot;</span>?
                <br /><br />
                This will permanently delete all samples, embeddings, and views associated with this dataset.
                <br /><br />
                <span className="text-red-400 font-semibold bg-red-950/30 px-2 py-1 rounded border border-red-900/50">⚠️ This action cannot be undone.</span>
              </p>

              <div className="flex gap-3 justify-end">
                <button
                  onClick={handleCancelDelete}
                  className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white rounded-lg transition-colors text-sm font-medium"
                >
                  Cancel
                </button>
                <button
                  onClick={handleConfirmDelete}
                  disabled={deletingId === confirmDelete.id}
                  className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 text-sm font-medium shadow-lg shadow-red-900/20"
                >
                  {deletingId === confirmDelete.id ? (
                    <>
                      <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                      Deleting...
                    </>
                  ) : (
                    'Delete Dataset'
                  )}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </ProtectedRoute>
  );
}
