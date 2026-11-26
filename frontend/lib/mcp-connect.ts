// Connecting an AI client (Claude Code, Codex, any MCP client) to this server's MCP server at /mcp
// (backend/mcp_door.py, docs/ASSISTANT.md): what the API Tokens page says about the MCP token kind, and what it shows
// after one is made.

/** The token kind's one line on the API Tokens page. */
export const MCP_TOKEN_HELP =
    "For an AI client such as Claude Code: it opens only the MCP server, which reads as you through the assistant's "
    + 'tools, and no other part of the API.';

/** Said beside the command: where the token ends up, and how to keep it out of there. */
export const MCP_TOKEN_KEPT =
    "The command leaves the token in your shell's history, and Claude Code keeps it in its configuration in plain text. "
    + "To keep it out of both, put the server in a project's .mcp.json by hand, with the header "
    + '"Authorization": "Bearer ${FRIDGE_MCP_TOKEN}", and set FRIDGE_MCP_TOKEN in your environment.';

/** The address an AI client connects to: this server's /mcp. */
export function mcpAddress(origin: string): string {
    return `${origin.replace(/\/+$/, '')}/mcp`;
}

/** The header an AI client sends with every request. */
export function mcpHeader(token: string): string {
    return `Authorization: Bearer ${token}`;
}

/**
 * Claude Code's command that adds this server, as its documentation gives it for a remote HTTP server with a header
 * (`claude mcp add --transport http <name> <url> --header "..."`).
 */
export function claudeCodeCommand(origin: string, token: string): string {
    return `claude mcp add --transport http freedomridge ${mcpAddress(origin)} --header "${mcpHeader(token)}"`;
}
