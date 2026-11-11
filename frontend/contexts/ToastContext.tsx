'use client';

import React, { createContext, useContext, useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Cross2Icon, CheckCircledIcon, ExclamationTriangleIcon, InfoCircledIcon } from '@radix-ui/react-icons';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface Toast {
    id: string;
    message: string;
    type: ToastType;
    duration?: number;
}

interface ToastContextType {
    addToast: (message: string, type: ToastType, duration?: number) => void;
    removeToast: (id: string) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export function ToastProvider({ children }: { children: React.ReactNode }) {
    const [toasts, setToasts] = useState<Toast[]>([]);

    const removeToast = useCallback((id: string) => {
        setToasts(prev => prev.filter(t => t.id !== id));
    }, []);

    const addToast = useCallback((message: string, type: ToastType, duration = 3000) => {
        const id = Math.random().toString(36).substring(2, 9);
        setToasts(prev => [...prev, { id, message, type, duration }]);

        if (duration > 0) {
            setTimeout(() => {
                removeToast(id);
            }, duration);
        }
    }, [removeToast]);

    return (
        <ToastContext.Provider value={{ addToast, removeToast }}>
            {children}
            <ToastContainer toasts={toasts} removeToast={removeToast} />
        </ToastContext.Provider>
    );
}

function ToastContainer({ toasts, removeToast }: { toasts: Toast[]; removeToast: (id: string) => void }) {
    const [mounted, setMounted] = useState(false);

    React.useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
        setMounted(true);
    }, []);

    if (!mounted) return null;

    return createPortal(
        <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
            {toasts.map(toast => (
                <ToastItem key={toast.id} toast={toast} onClose={() => removeToast(toast.id)} />
            ))}
        </div>,
        document.body
    );
}

function ToastItem({ toast, onClose }: { toast: Toast; onClose: () => void }) {
    const bgColors = {
        success: 'bg-zinc-900 border-green-500/50 text-green-100',
        error: 'bg-zinc-900 border-red-500/50 text-red-100',
        warning: 'bg-zinc-900 border-yellow-500/50 text-yellow-100',
        info: 'bg-zinc-900 border-cyan-500/50 text-cyan-100',
    };

    const icons = {
        success: <CheckCircledIcon className="w-5 h-5 text-green-500" />,
        error: <ExclamationTriangleIcon className="w-5 h-5 text-red-500" />,
        warning: <ExclamationTriangleIcon className="w-5 h-5 text-yellow-500" />,
        info: <InfoCircledIcon className="w-5 h-5 text-cyan-500" />,
    };

    return (
        <div className={`
            flex items-center gap-3 min-w-[300px] max-w-sm p-4 rounded-lg border shadow-lg 
            animate-in slide-in-from-right-full fade-in duration-300
            ${bgColors[toast.type]}
        `}>
            {icons[toast.type]}
            {/* A long unbroken message (a link to copy by hand) wraps and scrolls inside the toast. */}
            <p className="flex-1 max-h-60 overflow-y-auto wrap-anywhere text-sm font-medium">{toast.message}</p>
            <button
                onClick={onClose}
                className="p-1 hover:bg-white/10 rounded-full transition-colors"
            >
                <Cross2Icon className="w-4 h-4 opacity-70" />
            </button>
        </div>
    );
}

export function useToast() {
    const context = useContext(ToastContext);
    if (!context) {
        throw new Error('useToast must be used within a ToastProvider');
    }
    return context;
}
