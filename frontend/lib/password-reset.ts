// What an admin's password reset, a user's own password change, or deactivating an account did to the account's API
// tokens (POST /api/users/{id}/reset-password, POST /api/auth/change-password and PATCH /api/users/{id} with
// is_active false answer `revoked_tokens` by scope). A reset revokes them unless the admin unticks "Also revoke their
// API tokens"; a change only when the user ticks "Also revoke my API tokens". Follower and worker tokens are the
// account's too: whatever used one stops working.
export interface RevokedTokens {
    full?: number;
    follower?: number;
    worker?: number;
}

export const RESET_PASSWORD_HINT =
    'Signs them out and revokes their API tokens, including follower and worker tokens: followers using one stop '
    + "syncing until they're given a new token, and on-demand machines started under it end.";

// The hint with "Also revoke their API tokens" unticked.
export const RESET_PASSWORD_KEEP_TOKENS_HINT = 'Signs them out. Their API tokens are kept and still work.';

export const DEACTIVATE_HINT =
    'Deactivating revokes their API tokens for good, including follower and worker tokens: reactivating the account '
    + "doesn't bring them back.";

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

// "<done>, and N API tokens are revoked, including …", then what to do about the follower and worker tokens;
// `none` follows `done` when there were no tokens.
function revokedMessage(done: string, none: string, revoked: RevokedTokens | undefined): string {
    const follower = revoked?.follower ?? 0;
    const worker = revoked?.worker ?? 0;
    const total = (revoked?.full ?? 0) + follower + worker;
    if (!total) return `${done}${none}`;
    const parts = [follower && count(follower, 'follower token'), worker && count(worker, 'worker token')].filter(Boolean);
    let message = `${done}, and ${count(total, 'API token')} ${total === 1 ? 'is' : 'are'} revoked`;
    if (parts.length) message += `, including ${parts.join(' and ')}`;
    message += '.';
    if (follower) message += ` Give ${follower === 1 ? 'that follower' : 'those followers'} a new token (Settings → Followers).`;
    if (worker) message += ` ${worker === 1 ? 'A worker' : 'Workers'} using ${worker === 1 ? 'it' : 'them'} stopped.`;
    return message;
}

/**
 * The message shown after a reset: what was revoked, and what to do about the follower and worker tokens; or, when
 * the admin kept the tokens (`revokeTokens` false), that they still work.
 */
export function passwordResetMessage(revoked: RevokedTokens | undefined, revokeTokens = true): string {
    if (!revokeTokens) return 'Password reset. They are signed out; their API tokens still work.';
    return revokedMessage('Password reset. They are signed out', '; they had no API tokens.', revoked);
}

/** The message shown after a user changed their own password and asked for their API tokens to be revoked. */
export function passwordChangeMessage(revoked: RevokedTokens | undefined): string {
    return revokedMessage('Password changed', '. You had no API tokens.', revoked);
}

/** The message shown after deactivating an account, in the same words as a reset's. */
export function deactivationMessage(revoked: RevokedTokens | undefined): string {
    return revokedMessage('Account deactivated', '. They had no API tokens.', revoked);
}
