import { useState, useEffect } from 'react'
import { fmtDateTime } from '../lib/parseUTC'
import { connectWS } from '../lib/ws'

const STATUS_COLORS = { success: 'text-accent-green', failed: 'text-accent-orange', running: 'text-accent-cyan', pending: 'text-text-secondary' }

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

  const created = fmtDateTime(task.created_at)

  const renderStatus = () => {
    if (task.status === 'running') return (
      <div className="flex items-center gap-2">
        <span className="arc-dot" />
        <span className="hud-label text-accent-cyan">RUNNING</span>
      </div>
    )
    if (task.status === 'pending') return (
      <div className="flex items-center gap-2">
        <span className="arc-dot-dim" />
        <span className="hud-label">PENDING</span>
      </div>
    )
    if (task.status === 'success') return (
      <div className="flex items-center gap-2">
        <span className="arc-dot" style={{ animation: 'none' }} />
        <span className="hud-label text-accent-green">COMPLETE</span>
      </div>
    )
    if (task.status === 'failed') return (
      <div className="flex items-center gap-2">
        <span className="arc-dot-err" />
        <span className="hud-label text-accent-red">FAILED</span>
      </div>
    )
    return (
      <div className="flex items-center gap-2">
        <span className="arc-dot-dim" />
        <span className="hud-label">{String(task.status).toUpperCase()}</span>
      </div>
    )
  }

  const renderActions = () => {
    if (isRunning) {
      // Running/pending: immediate cancel, no confirm required
      return (
        <button onClick={handleCancel} disabled={busy}
          className="border border-accent-orange/50 text-accent-orange text-xs px-2 py-0.5 hover:bg-accent-orange/10 disabled:opacity-40">
          Cancel
        </button>
      )
    }

    if (confirmPending) {
      // Two-click confirm state: show CONFIRM + ABORT
      return (
        <>
          <button onClick={handleCancel} disabled={busy}
            className="border border-accent-red/70 text-accent-red text-xs px-2 py-0.5 hover:bg-accent-red/10 disabled:opacity-40">
            CONFIRM
          </button>
          <button onClick={handleAbortDelete} disabled={busy}
            className="border border-border-dark text-text-secondary text-xs px-2 py-0.5 hover:border-accent-cyan/40 disabled:opacity-40">
            ABORT
          </button>
        </>
      )
    }

    // Default non-running state: show DELETE (first click triggers confirm in parent)
    return (
      <button onClick={handleCancel} disabled={busy}
        className="border border-border-dark text-text-secondary text-xs px-2 py-0.5 hover:border-accent-orange/40 disabled:opacity-40">
        Delete
      </button>
    )
  }

  return (
    <div className="hud-panel-sm p-4 relative">
      <div className="flex items-center justify-between gap-2">
        {renderStatus()}
        <div className="flex gap-2">
          {isFailed && (
            <button onClick={handleRetry} disabled={busy}
              className="border border-accent-cyan/50 text-accent-cyan text-xs px-2 py-0.5 hover:bg-accent-cyan/10 disabled:opacity-40">
              Retry
            </button>
          )}
          {renderActions()}
        </div>
      </div>

      <p className="text-text-primary text-sm mt-2 leading-relaxed">{task.prompt}</p>

      {/* Live log line shown only while task is running */}
      {isRunning && (
        <div className="flex items-center gap-2 mt-1">
          <span className="arc-dot-err" style={{ width: '6px', height: '6px' }} />
          <span className="font-mono text-xs text-text-secondary truncate">
            {liveLog || 'PROCESSING...'}
          </span>
        </div>
      )}

      <div className="flex items-center gap-3 mt-2">
        <div className="font-mono text-xs text-text-secondary">
          {created}
        </div>
        {task.steps_taken > 0 && <div className="font-mono text-xs text-text-secondary">{task.steps_taken} step{task.steps_taken !== 1 ? 's' : ''}</div>}
        {(plan || result) && (
          <button onClick={() => setOpen(o => !o)} className="text-accent-cyan text-xs font-mono cursor-pointer hover:underline">
            {open ? '▲ hide' : '▼ show answer'}
          </button>
        )}
      </div>

      {errorMsg && (
        <div className="mt-2 text-accent-orange/80 text-xs font-mono bg-accent-orange/5 px-2 py-1 border-l-2 border-accent-orange/50">error: {errorMsg}</div>
      )}

      {/* Collapsed preview — first line of the last result step */}
      {!open && !errorMsg && Array.isArray(result) && result.length > 0 && (
        <div className="mt-2 text-text-secondary text-xs italic line-clamp-2">
          {String(result[result.length - 1]).split('\n').find(l => l.trim()) || ''}
        </div>
      )}

      {open && (
        <div className="border-t border-border-dark mt-3 pt-3 space-y-3">
          {plan && (
            <div>
              <div className="hud-label mb-1">Plan</div>
              <ol className="space-y-1">
                {plan.map(s => (
                  <li key={s.index} className="text-xs text-text-primary">
                    <span className="text-accent-cyan font-mono">{s.index}.</span> {s.description}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {Array.isArray(result) && result.length > 0 && (
            <div>
              <div className="hud-label mb-1">Results</div>
              <div className="space-y-2">
                {result.map((r, i) => (
                  <pre key={i} className="text-text-primary text-xs whitespace-pre-wrap bg-bg-primary p-2 border border-border-dark font-mono">{r}</pre>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
