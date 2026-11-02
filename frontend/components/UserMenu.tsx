'use client';

import React, { useState } from 'react';
import { useUser } from '@/contexts/UserContext';
import { ChangePasswordModal } from './ChangePasswordModal';
import { PersonIcon, GearIcon, ExitIcon } from '@radix-ui/react-icons';
import { useRouter } from 'next/navigation';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';

export function UserMenu() {
    const router = useRouter();
    const { user, logout } = useUser();
    const [showPasswordModal, setShowPasswordModal] = useState(false);

    if (!user) return null;

    return (
        <>
            <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>
                    <button
                        aria-label="User menu"
                        className="outline-none focus:ring-2 focus:ring-cyan-500 rounded-full"
                    >
                        <div className="h-8 w-8 rounded-full bg-gradient-to-tr from-cyan-500 to-violet-500 p-[1px]">
                            <div className="h-full w-full rounded-full bg-zinc-950 flex items-center justify-center">
                                <span className="text-xs font-bold text-white">
                                    {user.username.substring(0, 2).toUpperCase()}
                                </span>
                            </div>
                        </div>
                    </button>
                </DropdownMenu.Trigger>

                <DropdownMenu.Portal>
                    <DropdownMenu.Content
                        className="min-w-[200px] bg-zinc-900 border border-zinc-800 rounded-md shadow-xl py-1 z-[100] animate-in fade-in zoom-in-95 duration-100"
                        sideOffset={5}
                        align="end"
                    >
                        <div className="px-4 py-2 border-b border-zinc-800 mb-1">
                            <p className="text-sm font-medium text-white mb-1">{user.username}</p>
                            <p className="text-xs text-zinc-500">{user.role.replace('_', ' ')}</p>
                        </div>

                        {user.role === 'admin' && (
                            <DropdownMenu.Item
                                className="outline-none cursor-pointer"
                                onSelect={() => router.push('/users')}
                            >
                                <div className="w-full px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 hover:text-white flex items-center gap-2 transition-colors">
                                    <PersonIcon /> Manage Users
                                </div>
                            </DropdownMenu.Item>
                        )}

                        <DropdownMenu.Item
                            className="outline-none cursor-pointer"
                            onSelect={() => router.push('/settings/tokens')}
                        >
                            <div className="w-full px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 hover:text-white flex items-center gap-2 transition-colors">
                                <GearIcon /> API Tokens
                            </div>
                        </DropdownMenu.Item>

                        {!user.is_demo_account && (
                            <DropdownMenu.Item
                                className="outline-none cursor-pointer"
                                onSelect={() => setShowPasswordModal(true)}
                            >
                                <div className="w-full px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 hover:text-white flex items-center gap-2 transition-colors">
                                    <GearIcon /> Change Password
                                </div>
                            </DropdownMenu.Item>
                        )}

                        <DropdownMenu.Separator className="h-px bg-zinc-800 my-1" />

                        <DropdownMenu.Item
                            className="outline-none cursor-pointer"
                            onSelect={() => logout()}
                        >
                            <div className="w-full px-4 py-2 text-sm text-red-400 hover:bg-zinc-800 hover:text-red-300 flex items-center gap-2 transition-colors">
                                <ExitIcon /> Logout
                            </div>
                        </DropdownMenu.Item>
                    </DropdownMenu.Content>
                </DropdownMenu.Portal>
            </DropdownMenu.Root>

            <ChangePasswordModal
                open={showPasswordModal}
                onOpenChange={setShowPasswordModal}
            />
        </>
    );
}
