import { notifyManager } from '@tanstack/react-query';

// React Query schedules each query's change notification on its own timer, so a burst of results
// (say 300 streamed sidebar fields settling at once) renders once per result. Run every notification
// scheduled before the timer fires together, in one task, where React renders them once.
export function coalesceQueryNotifications(): void {
    let queued: Array<() => void> = [];
    notifyManager.setScheduler(notify => {
        queued.push(notify);
        if (queued.length > 1) return;
        setTimeout(() => {
            const run = queued;
            queued = [];
            for (const each of run) {
                try {
                    each();
                } catch (error) {
                    setTimeout(() => { throw error; });  // reported as before, without dropping the rest
                }
            }
        }, 0);
    });
}
