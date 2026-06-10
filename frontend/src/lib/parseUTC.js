// Backend emits naive timestamps without timezone info; treat them as UTC.
// The regex matches an existing Z suffix or a numeric offset (+05:00, -0700, etc.)
// so we never double-append Z when an offset is already present.
const HAS_TZ = /Z$|[+-]\d{2}:?\d{2}$/

/**
 * Parse a date string from the backend as UTC.
 * Returns a Date object (may be invalid — check isNaN(d.getTime())) or
 * an invalid Date for falsy input.
 *
 * @param {string|null|undefined} s
 * @returns {Date}
 */
export function parseUTC(s) {
  if (!s) return new Date(NaN)
  return new Date(HAS_TZ.test(s) ? s : s + 'Z')
}

/**
 * Format a backend timestamp as a locale time string (HH:MM).
 * Returns empty string when the value is missing or unparseable.
 *
 * @param {string|null|undefined} s
 * @returns {string}
 */
export function fmtTime(s) {
  const d = parseUTC(s)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString()
}

/**
 * Format a backend timestamp as a full locale date + time string.
 * Returns empty string when the value is missing or unparseable.
 *
 * @param {string|null|undefined} s
 * @returns {string}
 */
export function fmtDateTime(s) {
  const d = parseUTC(s)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString()
}
