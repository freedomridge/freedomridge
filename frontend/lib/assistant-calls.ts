// The "Recent calls" table on Settings → Assistant: the assistant's call log (GET /api/admin/assistant/calls,
// docs/ASSISTANT.md "The call log"), a page at a time.

export interface AssistantCall {
    id: number;
    user_id: number | null;  // null once the user is deleted
    username: string | null;
    door: 'mcp' | 'chat';
    tool: string;
    dataset_id: number | null;
    started_at: string;
    duration_ms: number;
    ok: boolean;
    error_code: string | null;
}

export interface AssistantCalls {
    calls: AssistantCall[];
    total: number;
    offset: number;
    next_offset: number | null;
}

/** How many calls one page shows. */
export const CALLS_PAGE = 50;

const OUTCOMES: Record<string, string> = {
    not_allowed: 'Not allowed',
    refused: 'Refused',
    not_found: 'Not found',
    invalid: 'Invalid arguments',
    busy: 'Busy',
    timeout: 'Took too long',
    failed: 'Server failed',
    unknown_tool: 'Unknown tool',
};

/** How a call ended, in a word or two. */
export function callOutcome(call: Pick<AssistantCall, 'ok' | 'error_code'>): string {
    if (call.ok) return 'OK';
    return OUTCOMES[call.error_code ?? ''] ?? call.error_code ?? 'Error';
}

/** How long a call took. */
export function callDuration(ms: number): string {
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** Who made a call: their name, or that the account is gone. */
export function callPerson(call: Pick<AssistantCall, 'username'>): string {
    return call.username ?? 'Deleted user';
}

/** The door a call came through. */
export function callDoor(door: string): string {
    return door === 'mcp' ? 'MCP' : door === 'chat' ? 'Chat' : door;
}

/** "Calls 1–50 of 312", or that there are none. */
export function callsShowing(page: Pick<AssistantCalls, 'offset' | 'total'>, shown: number): string {
    if (!page.total) return 'No calls in the last 30 days.';
    return `Calls ${(page.offset + 1).toLocaleString()}–${(page.offset + shown).toLocaleString()} of ${page.total.toLocaleString()}`;
}
