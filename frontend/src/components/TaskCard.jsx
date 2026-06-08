import { useState } from 'react'

const STATUS_COLORS = { success: 'text-accent-green', failed: 'text-accent-orange', running: 'text-accent-cyan', pending: 'text-text-secondary' }

function parsePlan(task) {
  try { return task.plan_json ? JSON.parse(task.plan_json) : null } catch { return null }
}
function parseResult(task) {
  try { return task.result_json ? JSON.parse(task.result_json) : null } catch { return null }
}

export default function TaskCard({ task, onCancel, onRetry }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const plan = parsePlan(task)
  const result = parseResult(task)
  const isRunning = task.status === 'running' || task.status === 'pending'
  const isFailed = task.status === 'failed'

  const handleCancel = async () => {
    setBusy(true)
    try { await onCancel?.(task.id) } finally { setBusy(false) }
  }
  const handleRetry = async () => {
    setBusy(true)
    try { await onRetry?.(task.id) } finally { setBusy(false) }
  }

  const errorMsg = result && !Array.isArray(result) && result.error

  return (
    <div className="bg-bg-card border border-border-dark rounded-lg p-4">
      <div className="flex items-start justify-between gap-2">
        <p className="text-text-primary text-sm flex-1">{task.prompt}</p>
        <span className={`text-xs font-mono flex-shrink-0 ${STATUS_COLORS[task.status] || 'text-text-secondary'}`}>{task.status}</span>
      </div>

      <div className="flex items-center gap-3 mt-2">
        <div className="text-text-secondary text-xs">{new Date(task.created_at.endsWith('Z') ? task.created_at : task.created_at + 'Z').toLocaleString()}</div>
        {task.steps_taken > 0 && <div className="text-text-secondary text-xs">{task.steps_taken} step{task.steps_taken !== 1 ? 's' : ''}</div>}
        {(plan || result) && (
          <button onClick={() => setOpen(o => !o)} className="text-accent-cyan text-xs font-mono font-bold hover:underline">
            {open ? '▲ hide' : '▼ show answer'}
          </button>
        )}
        <div className="ml-auto flex gap-2">
          {isRunning && (
            <button onClick={handleCancel} disabled={busy}
              className="text-xs font-mono px-2 py-1 rounded border border-accent-orange text-accent-orange hover:bg-accent-orange hover:text-bg-primary disabled:opacity-50">
              Cancel
            </button>
          )}
          {isFailed && (
            <button onClick={handleRetry} disabled={busy}
              className="text-xs font-mono px-2 py-1 rounded border border-accent-cyan text-accent-cyan hover:bg-accent-cyan hover:text-bg-primary disabled:opacity-50">
              Retry
            </button>
          )}
          {!isRunning && (
            <button onClick={handleCancel} disabled={busy}
              className="text-xs font-mono px-2 py-1 rounded border border-border-dark text-text-secondary hover:border-accent-orange hover:text-accent-orange disabled:opacity-50">
              Delete
            </button>
          )}
        </div>
      </div>

      {errorMsg && (
        <div className="mt-2 text-xs text-accent-orange font-mono">error: {errorMsg}</div>
      )}

      {/* Collapsed preview — first line of the last result step */}
      {!open && !errorMsg && Array.isArray(result) && result.length > 0 && (
        <div className="mt-2 text-text-secondary text-xs line-clamp-2 italic">
          {String(result[result.length - 1]).split('\n').find(l => l.trim()) || ''}
        </div>
      )}

      {open && (
        <div className="mt-3 border-t border-border-dark pt-3 space-y-3">
          {plan && (
            <div>
              <div className="text-text-secondary text-xs uppercase tracking-wider mb-1">Plan</div>
              <ol className="space-y-1">
                {plan.map(s => (
                  <li key={s.index} className="text-text-primary text-xs">
                    <span className="text-accent-cyan font-mono">{s.index}.</span> {s.description}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {Array.isArray(result) && result.length > 0 && (
            <div>
              <div className="text-text-secondary text-xs uppercase tracking-wider mb-1">Results</div>
              <div className="space-y-2">
                {result.map((r, i) => (
                  <pre key={i} className="text-text-primary text-xs whitespace-pre-wrap bg-bg-primary rounded p-2 border border-border-dark">{r}</pre>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
