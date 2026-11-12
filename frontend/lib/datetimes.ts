// Metadata datetimes as the server reads them (backend/datetimes.py): `YYYY-MM-DD`, then optionally `T` or a space,
// `hh:mm`, `:ss`, a `.fraction`, and an offset `Z`, `±hh`, `±hhmm` or `±hh:mm`. No offset means UTC, and a date alone
// is midnight UTC. A browser reads `2026-01-01T10:00` (and a space-separated time) in its own zone, so the controls
// that show or send a datetime read it here instead.
const DATETIME = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/;

/** The instant a datetime text names, in epoch milliseconds (fraction digits past the third dropped); NaN for other text. */
export function datetimeMs(text: string): number {
    const match = DATETIME.exec(text);
    if (!match) return NaN;
    const [, date, clock = '00:00', seconds = '00', fraction = '', offset = 'Z'] = match;
    const digits = offset.slice(1).replace(':', '');
    const zone = offset === 'Z' ? 'Z' : `${offset[0]}${digits.slice(0, 2)}:${digits.slice(2) || '00'}`;
    return Date.parse(`${date}T${clock}:${seconds}.${fraction.padEnd(3, '0').slice(0, 3)}${zone}`);
}

/** A datetime text as the UTC `YYYY-MM-DDTHH:MM` a `datetime-local` input shows; other text cut to that length. */
export function utcMinute(text: string): string {
    const ms = datetimeMs(text);
    return Number.isFinite(ms) ? new Date(ms).toISOString().slice(0, 16) : text.slice(0, 16);
}
