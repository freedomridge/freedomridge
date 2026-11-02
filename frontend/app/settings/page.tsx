'use client';

import { useState } from 'react';
import { Header } from '@/components/header';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { useUser } from '@/contexts/UserContext';
import Link from 'next/link';
import { Key, Users, Shield, Cloud, Activity } from 'lucide-react';
import { ChangePasswordModal } from '@/components/ChangePasswordModal';

export default function SettingsPage() {
    const { user } = useUser();
    const [showPasswordModal, setShowPasswordModal] = useState(false);

    return (
        <ProtectedRoute>
            <div className="min-h-screen bg-zinc-950 flex flex-col">
                <Header />

                <main className="flex-1 container mx-auto px-6 py-8">
                    <div className="mb-8">
                        <h1 className="text-3xl font-bold text-white">Settings</h1>
                        <p className="text-zinc-400 mt-1">Manage your account and application preferences</p>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                        {/* API Tokens Card */}
                        <Link
                            href="/settings/tokens"
                            className="group block p-6 bg-zinc-900/50 border border-zinc-800 rounded-xl hover:border-cyan-500/50 hover:ring-1 hover:ring-cyan-500/50 transition-all shadow-lg hover:shadow-cyan-900/10"
                        >
                            <div className="flex items-center gap-4 mb-4">
                                <div className="p-3 bg-cyan-500/10 rounded-lg text-cyan-400 group-hover:bg-cyan-500/20 transition-colors">
                                    <Key size={24} />
                                </div>
                                <h3 className="text-xl font-semibold text-white group-hover:text-cyan-400 transition-colors">
                                    API Tokens
                                </h3>
                            </div>
                            <p className="text-zinc-400 text-sm">
                                Manage your personal access tokens for the Python SDK and API access.
                            </p>
                        </Link>

                        {/* User Management Card (Admin Only) */}
                        {(user?.role === 'admin') && (
                            <Link
                                href="/users"
                                className="group block p-6 bg-zinc-900/50 border border-zinc-800 rounded-xl hover:border-purple-500/50 hover:ring-1 hover:ring-purple-500/50 transition-all shadow-lg hover:shadow-purple-900/10"
                            >
                                <div className="flex items-center gap-4 mb-4">
                                    <div className="p-3 bg-purple-500/10 rounded-lg text-purple-400 group-hover:bg-purple-500/20 transition-colors">
                                        <Users size={24} />
                                    </div>
                                    <h3 className="text-xl font-semibold text-white group-hover:text-purple-400 transition-colors">
                                        User Management
                                    </h3>
                                </div>
                                <p className="text-zinc-400 text-sm">
                                    Administer users, roles, and permissions for the organization.
                                </p>
                            </Link>
                        )}

                        {/* Storage Configuration Card (Admin Only) */}
                        {(user?.role === 'admin') && (
                            <Link
                                href="/settings/storage"
                                className="group block p-6 bg-zinc-900/50 border border-zinc-800 rounded-xl hover:border-orange-500/50 hover:ring-1 hover:ring-orange-500/50 transition-all shadow-lg hover:shadow-orange-900/10"
                            >
                                <div className="flex items-center gap-4 mb-4">
                                    <div className="p-3 bg-orange-500/10 rounded-lg text-orange-400 group-hover:bg-orange-500/20 transition-colors">
                                        <Cloud size={24} />
                                    </div>
                                    <h3 className="text-xl font-semibold text-white group-hover:text-orange-400 transition-colors">
                                        Storage Configuration
                                    </h3>
                                </div>
                                <p className="text-zinc-400 text-sm">
                                    Configure S3 buckets for remote dataset storage.
                                </p>
                            </Link>
                        )}

                        {/* System Status Card (Admin Only) */}
                        {(user?.role === 'admin') && (
                            <Link
                                href="/settings/status"
                                data-testid="settings-system-status"
                                className="group block p-6 bg-zinc-900/50 border border-zinc-800 rounded-xl hover:border-blue-500/50 hover:ring-1 hover:ring-blue-500/50 transition-all shadow-lg hover:shadow-blue-900/10"
                            >
                                <div className="flex items-center gap-4 mb-4">
                                    <div className="p-3 bg-blue-500/10 rounded-lg text-blue-400 group-hover:bg-blue-500/20 transition-colors">
                                        <Activity size={24} />
                                    </div>
                                    <h3 className="text-xl font-semibold text-white group-hover:text-blue-400 transition-colors">
                                        System Status
                                    </h3>
                                </div>
                                <p className="text-zinc-400 text-sm">
                                    Disk space, database health, and which code the server is running.
                                </p>
                            </Link>
                        )}

                        {/* Security & Profile Card */}
                        {!user?.is_demo_account && <button
                            onClick={() => setShowPasswordModal(true)}
                            className="group block w-full text-left p-6 bg-zinc-900/50 border border-zinc-800 rounded-xl hover:border-green-500/50 hover:ring-1 hover:ring-green-500/50 transition-all shadow-lg hover:shadow-green-900/10"
                        >
                            <div className="flex items-center gap-4 mb-4">
                                <div className="p-3 bg-green-500/10 rounded-lg text-green-400 group-hover:bg-green-500/20 transition-colors">
                                    <Shield size={24} />
                                </div>
                                <h3 className="text-xl font-semibold text-white group-hover:text-green-400 transition-colors">
                                    Security & Profile
                                </h3>
                            </div>
                            <p className="text-zinc-400 text-sm">
                                Change your password and manage account security settings.
                            </p>
                        </button>}
                    </div>

                    {!user?.is_demo_account && <ChangePasswordModal
                        open={showPasswordModal}
                        onOpenChange={setShowPasswordModal}
                    />}
                </main>
            </div>
        </ProtectedRoute>
    );
}
