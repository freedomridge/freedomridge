'use client';

import Link from 'next/link';
import Image from 'next/image';
import { usePathname } from 'next/navigation';
import { UserMenu } from '@/components/UserMenu';
import { Wordmark } from '@/components/wordmark';
import { LowDiskBanner } from '@/components/system-status';

export function Header() {
    const pathname = usePathname();

    return (
        <header className="sticky top-0 z-50 w-full border-b border-zinc-800 bg-zinc-950/80 backdrop-blur supports-[backdrop-filter]:bg-zinc-950/60">
            <div className="px-6 flex h-16 items-center">
                <div className="mr-4 flex">
                    <Link href="/" className="mr-8 flex items-center space-x-3 group">
                        <div className="relative h-8 w-8 overflow-hidden rounded-lg shadow-lg shadow-cyan-500/20 ring-1 ring-white/10 group-hover:ring-cyan-500/50 transition-all">
                            <Image src="/logo.png" alt="Freedom Ridge" fill sizes="32px" className="object-cover" />
                        </div>
                        <span className="hidden font-bold sm:inline-block text-lg">
                            <Wordmark />
                        </span>
                    </Link>
                    <nav className="flex items-center space-x-6 text-sm font-medium">
                        <Link
                            href="/"
                            className={`transition-colors hover:text-cyan-400 ${pathname === '/' ? 'text-white' : 'text-zinc-400'}`}
                        >
                            Datasets
                        </Link>
                        <Link
                            href="/settings"
                            className={`transition-colors hover:text-cyan-400 ${pathname?.startsWith('/settings') ? 'text-white' : 'text-zinc-400'}`}
                        >
                            Settings
                        </Link>
                    </nav>
                </div>
                <div className="flex flex-1 items-center justify-between space-x-2 md:justify-end">
                    <div className="w-full flex-1 md:w-auto md:flex-none">
                        {/* Search placeholder */}
                    </div>
                    <div className="flex items-center gap-4">
                        <UserMenu />
                    </div>
                </div>
            </div>
            <LowDiskBanner />
        </header>
    );
}
