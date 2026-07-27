// Display formatters shared by every page/component. Timestamps go through
// parseUTC because the backend emits naive UTC (datetime.utcnow().isoformat(),
// no trailing 'Z') — a bare new Date(iso) reads it as local time and skews
// relative ages by the UTC offset.
import { parseUTC } from './parseUTC'

/**
 * Human "N ago" age of a backend timestamp.
 * Returns '' when the value is missing or unparseable.
 *
 * @param {string|null|undefined} iso
 * @returns {string}
 */
export function relativeTime(iso) {
  const d = parseUTC(iso)
  if (isNaN(d.getTime())) return ''
  const diff = Math.floor((Date.now() - d.getTime()) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

/**
 * Milliseconds between two backend timestamps, or null when either is
 * missing/unparseable.
 *
 * @param {string|null|undefined} startedAt
 * @param {string|null|undefined} endedAt
 * @returns {number|null}
 */
export function durationMs(startedAt, endedAt) {
  const s = parseUTC(startedAt).getTime()
  const e = parseUTC(endedAt).getTime()
  if (isNaN(s) || isNaN(e)) return null
  return e - s
}

/**
 * Format a duration in milliseconds — sub-second stays in ms, above that
 * switches to seconds with 2 decimals.
 *
 * @param {number|null|undefined} ms
 * @param {string} fallback rendered for a missing value
 * @returns {string}
 */
export function fmtMs(ms, fallback = '—') {
  if (ms === null || ms === undefined) return fallback
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`
}

/**
 * Format a dollar amount — sub-dollar keeps 4 decimals so per-call LLM costs
 * don't all render as $0.00.
 *
 * @param {number|string|null|undefined} n
 * @param {string|null} fallback rendered for a missing value
 * @returns {string|null}
 */
export function fmtUsd(n, fallback = '$—') {
  if (n === null || n === undefined) return fallback
  const v = Number(n)
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

/**
 * Format a percentage with one decimal.
 *
 * @param {number|null|undefined} n
 * @param {string} fallback rendered for a missing value
 * @returns {string}
 */
export function fmtPct(n, fallback = '—%') {
  if (n === null || n === undefined) return fallback
  return `${Number(n).toFixed(1)}%`
}
