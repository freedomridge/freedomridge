'use client';

import React, { useEffect, useState } from 'react';
import { useUser } from '@/contexts/UserContext';
import { useRouter } from 'next/navigation';
import { ChangePasswordModal } from './ChangePasswordModal';
import { loginPath } from '@/lib/links';

export function ProtectedRoute({ children }: { children: React.ReactNode }) {
    const { user, isLoading, isAuthenticated } = useUser();
    const router = useRouter();
    const [showPasswordModal, setShowPasswordModal] = useState(false);
    const mustChangePassword = !!user?.must_change_password;

    useEffect(() => {
        if (!isLoading && !isAuthenticated) {
            router.push(loginPath(window.location));  // back here after signing in
        }
    }, [isLoading, isAuthenticated, router]);

    useEffect(() => {
        if (mustChangePassword) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate sync: open the modal when the backend flags a required password change
            setShowPasswordModal(true);
        }
    }, [mustChangePassword]);

    // When backend clears the requirement, close the modal
    useEffect(() => {
        if (!mustChangePassword && showPasswordModal) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate sync: close the modal once the backend clears the password-change requirement
            setShowPasswordModal(false);
        }
    }, [mustChangePassword, showPasswordModal]);

    // Keep the modal open whenever the backend says password change is required
    useEffect(() => {
        if (mustChangePassword && !showPasswordModal) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate sync: re-open the modal if it was closed while a password change is still required
            setShowPasswordModal(true);
        }
    }, [mustChangePassword, showPasswordModal]);

    if (isLoading) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-zinc-950 text-zinc-400">
                Loading...
            </div>
        );
    }

    if (!isAuthenticated) {
        return null; // Will redirect
    }

    return (
        <>
            {children}
            <ChangePasswordModal
                open={mustChangePassword ? true : showPasswordModal}
                forceRequired={mustChangePassword}
                onOpenChange={(open) => {
                    // Prevent closing if password change is mandatory
                    if (mustChangePassword && !open) {
                        return;
                    }
                    setShowPasswordModal(open);
                }}
            />
        </>
    );
}
