// What an admin's password reset did to the account's API tokens (POST /api/users/{id}/reset-password answers
// `revoked_tokens` by scope). Follower and worker tokens are the account's too: whatever used one stops working.
export interface RevokedTokens {
    full?: number;
    follower?: number;
    worker?: number;
}

export const RESET_PASSWORD_HINT =
    'Signs them out and revokes their API tokens, including follower and worker tokens: followers using one stop '
    + "syncing until they're given a new token, and on-demand machines started under it end.";

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

/** The message shown after a reset: what was revoked, and what to do about the follower and worker tokens. */
export function passwordResetMessage(revoked: RevokedTokens | undefined): string {
    const follower = revoked?.follower ?? 0;
    const worker = revoked?.worker ?? 0;
    const total = (revoked?.full ?? 0) + follower + worker;
    if (!total) return 'Password reset. They are signed out; they had no API tokens.';
    const parts = [follower && count(follower, 'follower token'), worker && count(worker, 'worker token')].filter(Boolean);
    let message = `Password reset. They are signed out, and ${count(total, 'API token')} ${total === 1 ? 'is' : 'are'} revoked`;
    if (parts.length) message += `, including ${parts.join(' and ')}`;
    message += '.';
    if (follower) message += ` Give ${follower === 1 ? 'that follower' : 'those followers'} a new token (Settings → Followers).`;
    if (worker) message += ` ${worker === 1 ? 'A worker' : 'Workers'} using ${worker === 1 ? 'it' : 'them'} stopped.`;
    return message;
}
