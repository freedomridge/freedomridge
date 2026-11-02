'use client';

import React from 'react';
import { useUser } from '@/contexts/UserContext';

type Role = 'admin' | 'power_user' | 'guest';

interface PermissionGateProps {
    role: Role;
    children: React.ReactNode;
    fallback?: React.ReactNode;
}

const ROLE_HIERARCHY: Record<Role, Role[]> = {
    'admin': ['admin'],
    'power_user': ['admin', 'power_user'],
    'guest': ['admin', 'power_user', 'guest']
};

export function PermissionGate({ role, children, fallback = null }: PermissionGateProps) {
    const { user } = useUser();

    if (!user) return <>{fallback}</>;

    const allowedRoles = ROLE_HIERARCHY[role];

    if (allowedRoles.includes(user.role)) {
        return <>{children}</>;
    }

    return <>{fallback}</>;
}
