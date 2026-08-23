import { useState, useEffect } from 'react'
import { parseUTC } from '../lib/parseUTC'
import { connectWS } from '../lib/ws'
import StatusDot from './StatusDot'

const TONE = {
  success: { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)', label: 'COMPLETE' },
  failed:  { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)', label: 'FAILED' },
  running: { c: 'var(--accent)', bg: 'var(--ac-dim)', bd: 'var(--ac-line)', label: 'RUNNING' },
  pending: { c: '#a6a399', bg: 'rgba(180,178,170,0.08)', bd: 'rgba(180,178,170,0.14)', label: 'PENDING' },
  stopped: { c: '#98958c', bg: 'rgba(180,178,170,0.08)', bd: 'rgba(180,178,170,0.14)', label: 'STOPPED' },
}

function toneKey(status) {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'failed'
  if (status === 'running') return 'running'
  if (status === 'pending') return 'pending'
  if (status === 'stopped' || status === 'cancelled') return 'stopped'
  return 'pending'
}

function relativeTime(s) {
  if (!s) return ''
  const d = parseUTC(s)
  if (isNaN(d.getTime())) return ''
  const diff = Math.floor((Date.now() - d.getTime()) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function parsePlan(task) {
  try { return task.plan_json ? JSON.parse(task.plan_json) : null } catch { return null }
}
function parseResult(task) {
  try { return task.result_json ? JSON.parse(task.result_json) : null } catch { return null }
}

export default function TaskCard({ task, onCancel, onRetry, confirmPending, onAbortDelete }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [liveLog, setLiveLog] = useState(null)
  const plan = parsePlan(task)
  const result = parseResult(task)
  const isRunning = task.status === 'running' || task.status === 'pending'
  const isFailed = task.status === 'failed'

  const tk = toneKey(task.status)
  const t = TONE[tk]

  // Subscribe to live WebSocket logs while the task is running.
  // WS messages are global (server fans out all logs); we show the latest
  // line as a generic activity indicator for any running task.
  useEffect(() => {
    if (!isRunning) {
      setLiveLog(null)
      return
    }
    const off = connectWS(msg => setLiveLog(msg))
    return () => {
      off()
      setLiveLog(null)
    }
  }, [isRunning, task.id])

  const handleCancel = async () => {
    setBusy(true)
    try { await onCancel?.(task.id) } finally { setBusy(false) }
  }
  const handleRetry = async () => {
    setBusy(true)
    try { await onRetry?.(task.id) } finally { setBusy(false) }
  }
  const handleAbortDelete = () => {
    onAbortDelete?.(task.id)
  }

  const errorMsg = result && !Array.isArray(result) && result.error

  // Preview line content
  const previewContent = (() => {
    if (isFailed) return errorMsg ? `error: ${errorMsg}` : 'error: verify_rejected'
    if (task.status === 'success' && Array.isArray(result) && result.length > 0) {
      return String(result[result.length - 1]).split('\n').find(l => l.trim()) || ''
    }
    if (isRunning) return liveLog || 'Running…'
    return null
  })()

  const showPreview = previewContent && (isFailed || task.status === 'success' || isRunning)

  const relTime = relativeTime(task.created_at)
  const steps = task.steps_taken > 0 ? `${task.steps_taken} step${task.steps_taken !== 1 ? 's' : ''}` : null
  const metaLine = [relTime, steps].filter(Boolean).join(' · ')

  return (
    <div style={{
      background: 'linear-gradient(180deg,rgba(255,255,255,0.022),rgba(255,255,255,0)),#1c1c1c',
      border: '1px solid rgba(180,178,170,0.10)',
      borderRadius: '6px',
      padding: '16px 18px',
    }}>
      {/* Top row */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '10px',
        flexWrap: 'wrap',
        marginBottom: '10px',
      }}>
        {/* Status chip */}
        <div style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '7px',
          padding: '4px 10px',
          borderRadius: '4px',
          background: t.bg,
          border: `1px solid ${t.bd}`,
        }}>
          <StatusDot
            color={t.c}
            size={6}
            glow={false}
            pulse={tk === 'running'}
          />
          <span style={{
            fontSize: '10px',
            letterSpacing: '0.1em',
            fontWeight: 700,
            color: t.c,
            fontFamily: "'Archivo', sans-serif",
          }}>
            {t.label}
          </span>
        </div>

        {/* Meta: time + steps */}
        {metaLine && (
          <span style={{ fontSize: '11px', color: '#7a776d', fontFamily: "'IBM Plex Mono', monospace" }}>
            {metaLine}
          </span>
        )}
      </div>

      {/* Description */}
      <p style={{ fontSize: '14px', color: '#d9d6cd', lineHeight: 1.6, margin: 0 }}>
        {task.prompt}
      </p>

      {/* Action row */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: '10px',
        marginTop: '12px',
      }}>
        {/* Left: preview line */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {showPreview && (
            <span style={{
              fontFamily: "'IBM Plex Mono', monospace",
              fontSize: '12px',
              color: t.c,
              padding: '6px 10px',
              borderRadius: '4px',
              background: t.bg,
              borderLeft: `2px solid ${t.c}`,
              display: 'block',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>
              {previewContent}
            </span>
          )}
        </div>

        {/* Right: action buttons */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          {/* Retry — only if failed */}
          {isFailed && (
            <button
              onClick={handleRetry}
              disabled={busy}
              style={{
                border: '1px solid var(--ac-line)',
                background: 'transparent',
                color: 'var(--accent)',
                padding: '6px 13px',
                borderRadius: '4px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: busy ? 'not-allowed' : 'pointer',
                opacity: busy ? 0.4 : 1,
                fontFamily: "'Archivo', sans-serif",
              }}
            >
              Retry
            </button>
          )}

          {/* Delete / Confirm / Abort */}
          {!isRunning && (
            confirmPending ? (
              <>
                <button
                  onClick={handleCancel}
                  disabled={busy}
                  style={{
                    border: '1px solid #fb7185',
                    background: '#fb7185',
                    color: '#131313',
                    padding: '6px 13px',
                    borderRadius: '4px',
                    fontSize: '12px',
                    fontWeight: 700,
                    cursor: busy ? 'not-allowed' : 'pointer',
                    opacity: busy ? 0.4 : 1,
                    fontFamily: "'Archivo', sans-serif",
                  }}
                >
                  CONFIRM
                </button>
                <button
                  onClick={handleAbortDelete}
                  disabled={busy}
                  style={{
                    border: '1px solid rgba(180,178,170,0.20)',
                    background: 'transparent',
                    color: '#98958c',
                    padding: '6px 13px',
                    borderRadius: '4px',
                    fontSize: '12px',
                    fontWeight: 600,
                    cursor: busy ? 'not-allowed' : 'pointer',
                    opacity: busy ? 0.4 : 1,
                    fontFamily: "'Archivo', sans-serif",
                  }}
                >
                  ABORT
                </button>
              </>
            ) : (
              <button
                onClick={handleCancel}
                disabled={busy}
                style={{
                  border: '1px solid rgba(251,113,133,0.35)',
                  background: 'transparent',
                  color: '#fb7185',
                  padding: '6px 13px',
                  borderRadius: '4px',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: busy ? 'not-allowed' : 'pointer',
                  opacity: busy ? 0.4 : 1,
                  fontFamily: "'Archivo', sans-serif",
                }}
              >
                Delete
              </button>
            )
          )}

          {/* Cancel — only if running */}
          {isRunning && (
            <button
              onClick={handleCancel}
              disabled={busy}
              style={{
                border: '1px solid rgba(180,178,170,0.20)',
                background: 'transparent',
                color: '#98958c',
                padding: '6px 13px',
                borderRadius: '4px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: busy ? 'not-allowed' : 'pointer',
                opacity: busy ? 0.4 : 1,
                fontFamily: "'Archivo', sans-serif",
              }}
            >
              Cancel
            </button>
          )}

          {/* Expand toggle */}
          {(plan || (Array.isArray(result) && result.length > 0)) && (
            <button
              onClick={() => setOpen(o => !o)}
              style={{
                border: '1px solid rgba(255,138,61,0.20)',
                background: 'transparent',
                color: 'var(--accent)',
                padding: '6px 13px',
                borderRadius: '4px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: 'pointer',
                fontFamily: "'IBM Plex Mono', monospace",
              }}
            >
              {open ? '▲ hide' : '▼ details'}
            </button>
          )}
        </div>
      </div>

      {/* Expanded detail block */}
      {open && (
        <div style={{
          borderTop: '1px solid rgba(180,178,170,0.10)',
          marginTop: '12px',
          paddingTop: '12px',
          display: 'flex',
          flexDirection: 'column',
          gap: '12px',
        }}>
          {plan && (
            <div>
              <div style={{
                fontSize: '10px',
                letterSpacing: '0.12em',
                fontWeight: 700,
                color: '#7a776d',
                textTransform: 'uppercase',
                marginBottom: '8px',
              }}>
                Plan
              </div>
              <ol style={{ margin: 0, padding: '0 0 0 18px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                {plan.map(s => (
                  <li key={s.index} style={{ fontSize: '12px', color: '#b3b0a6', lineHeight: 1.5 }}>
                    <span style={{ color: 'var(--accent)', fontFamily: "'IBM Plex Mono', monospace" }}>{s.index}.</span>{' '}
                    {s.description}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {Array.isArray(result) && result.length > 0 && (
            <div>
              <div style={{
                fontSize: '10px',
                letterSpacing: '0.12em',
                fontWeight: 700,
                color: '#7a776d',
                textTransform: 'uppercase',
                marginBottom: '8px',
              }}>
                Results
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {result.map((r, i) => (
                  <pre key={i} style={{
                    background: '#161615',
                    borderRadius: '4px',
                    padding: '10px 12px',
                    fontFamily: "'IBM Plex Mono', monospace",
                    fontSize: '11px',
                    color: '#a6a399',
                    overflow: 'auto',
                    margin: 0,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}>
                    {r}
                  </pre>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
