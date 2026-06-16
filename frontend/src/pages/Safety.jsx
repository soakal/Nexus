import { useState, useEffect, useCallback } from 'react'
import { ShieldCheck } from 'lucide-react'
import { api } from '../lib/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtUsd(n) {
  if (n === null || n === undefined) return '$—'
  const v = Number(n)
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

function fmtPct(n) {
  if (n === null || n === undefined) return '—%'
  return `${Number(n).toFixed(1)}%`
}

function relativeTime(isoStr) {
  if (!isoStr) return ''
  const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function verdictColor(verdict) {
  switch ((verdict || '').toLowerCase()) {
    case 'success':  return 'text-accent-cyan'
    case 'failure':  return 'text-red-400'
    case 'partial':  return 'text-accent-orange'
    default:         return 'text-text-secondary'
  }
}

function decisionColor(decision) {
  switch ((decision || '').toLowerCase()) {
    case 'allowed':
    case 'executed':  return 'text-accent-cyan'
    case 'needs_confirm': return 'text-accent-orange'
    default:          return 'text-red-400'
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function SpendBar({ spend, budget }) {
  const pct = budget > 0 ? Math.min(100, (spend / budget) * 100) : 0
  const barColor = pct >= 100 ? '#ff2d2d' : pct >= 80 ? '#ff9500' : '#00d4ff'
  return (
    <div className="mt-3">
      <div
        className="relative h-3 rounded-full overflow-hidden"
        style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(0,212,255,0.18)' }}
      >
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct}%`, background: barColor, boxShadow: `0 0 6px ${barColor}` }}
        />
      </div>
      <div className="flex justify-between font-mono text-xs text-text-secondary mt-1">
        <span>
          {fmtUsd(spend)} / {fmtUsd(budget)}
        </span>
        <span style={{ color: barColor }}>{fmtPct(pct)}</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Safety() {
  const [status, setStatus]   = useState(null)
  const [outcomes, setOutcomes] = useState(null)
  const [actions, setActions]  = useState(null)
  const [toggling, setToggling] = useState(false)

  // Budget editor state
  const [dailyInput, setDailyInput]     = useState('')
  const [perTaskInput, setPerTaskInput] = useState('')
  const [budgetSaved, setBudgetSaved]   = useState(false)
  const [budgetErr, setBudgetErr]       = useState('')

  const load = useCallback(() => {
    api.safety.status().then(s => {
      setStatus(s)
      // Pre-fill budget inputs only if not currently editing
      setDailyInput(v => v || (s.daily_budget_usd != null ? String(s.daily_budget_usd) : ''))
      setPerTaskInput(v => v || (s.per_task_budget_usd != null ? String(s.per_task_budget_usd) : ''))
    }).catch(() => {})
    api.safety.outcomes(20).then(setOutcomes).catch(() => {})
    api.safety.actions(20).then(setActions).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load])

  async function handleToggle() {
    if (!status || toggling) return
    setToggling(true)
    try {
      if (status.autonomy_enabled) {
        await api.safety.pause()
      } else {
        await api.safety.resume()
      }
      await api.safety.status().then(setStatus).catch(() => {})
    } catch {
      // swallow; load() will refresh state
    } finally {
      setToggling(false)
      load()
    }
  }

  async function handleSaveBudget() {
    setBudgetErr('')
    setBudgetSaved(false)
    const daily   = parseFloat(dailyInput)
    const perTask = parseFloat(perTaskInput)
    if (!isFinite(daily) || daily <= 0) {
      setBudgetErr('Daily budget must be a positive number.')
      return
    }
    if (!isFinite(perTask) || perTask <= 0) {
      setBudgetErr('Per-task budget must be a positive number.')
      return
    }
    try {
      await api.safety.setBudget(daily, perTask)
      setBudgetSaved(true)
      setTimeout(() => setBudgetSaved(false), 2500)
      load()
    } catch {
      setBudgetErr('Failed to save. Check connection.')
    }
  }

  const autonomyOn = status?.autonomy_enabled ?? null

  return (
    <div className="p-4 md:p-6 max-w-4xl">
      <h1 className="page-header mb-6">SAFETY &amp; GOVERNANCE</h1>

      {/* ------------------------------------------------------------------ */}
      {/* Kill switch + status                                                 */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">AUTONOMY CONTROL</div>

      <div className="hud-panel-sm p-4 mb-6">
        {status === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : (
          <>
            {/* State line */}
            <div className="flex items-center gap-3 mb-4">
              <ShieldCheck
                size={20}
                style={{ color: autonomyOn ? '#00d4ff' : '#ff2d2d', filter: autonomyOn ? 'drop-shadow(0 0 6px rgba(0,212,255,0.7))' : 'drop-shadow(0 0 6px rgba(255,45,45,0.7))' }}
              />
              <span
                className={`font-mono text-sm font-bold tracking-widest ${autonomyOn ? 'text-accent-cyan glow-cyan-text' : 'text-red-400'}`}
              >
                {autonomyOn ? 'AUTONOMY ENABLED' : 'AUTONOMY PAUSED (KILL SWITCH ON)'}
              </span>
              <span className={autonomyOn ? 'arc-dot' : 'arc-dot-err'} />
            </div>

            {/* Toggle button */}
            <div className="flex items-center gap-4">
              {autonomyOn ? (
                <button
                  onClick={handleToggle}
                  disabled={toggling}
                  className="glow-btn-gold px-4 py-2 text-xs tracking-widest disabled:opacity-40"
                >
                  {toggling ? 'PAUSING...' : 'PAUSE / KILL SWITCH'}
                </button>
              ) : (
                <button
                  onClick={handleToggle}
                  disabled={toggling}
                  className="glow-btn px-4 py-2 text-xs tracking-widest disabled:opacity-40"
                  style={{ boxShadow: '0 0 10px rgba(0,212,255,0.35)' }}
                >
                  {toggling ? 'RESUMING...' : 'RESUME AUTONOMY'}
                </button>
              )}

              {/* Scheduler dot */}
              <div className="flex items-center gap-2">
                <span className={status.scheduler_running ? 'arc-dot' : 'arc-dot-warn'} />
                <span className="hud-label">
                  SCHEDULER {status.scheduler_running ? 'RUNNING' : 'PAUSED'}
                </span>
              </div>
            </div>
          </>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Spend meter                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">TODAY&apos;S SPEND</div>

      <div className="hud-panel-sm p-4 mb-6">
        {status === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : (
          <SpendBar spend={status.today_spend_usd ?? 0} budget={status.daily_budget_usd ?? 25} />
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Budget caps editor                                                   */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">BUDGET CAPS</div>

      <div className="hud-panel-sm p-4 mb-6">
        {status === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="hud-label mb-1 block">DAILY LIMIT (USD)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  value={dailyInput}
                  onChange={e => { setDailyInput(e.target.value); setBudgetSaved(false); setBudgetErr('') }}
                  className="hud-input w-full font-mono"
                />
              </div>
              <div>
                <label className="hud-label mb-1 block">PER-TASK LIMIT (USD)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  value={perTaskInput}
                  onChange={e => { setPerTaskInput(e.target.value); setBudgetSaved(false); setBudgetErr('') }}
                  className="hud-input w-full font-mono"
                />
              </div>
            </div>

            <div className="flex items-center gap-3">
              <button
                onClick={handleSaveBudget}
                className="glow-btn-gold px-4 py-2 text-xs tracking-widest"
              >
                SAVE CAPS
              </button>
              {budgetSaved && (
                <div className="flex items-center gap-1.5">
                  <span className="arc-dot" />
                  <span className="text-accent-cyan text-xs font-mono">SAVED</span>
                </div>
              )}
              {budgetErr && (
                <span className="text-red-400 text-xs font-mono">{budgetErr}</span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Recent verdicts                                                      */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">RECENT VERDICTS</div>

      <div className="hud-panel-sm p-4 mb-6">
        {outcomes === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : outcomes.length === 0 ? (
          <div className="hud-label opacity-40">NO VERDICTS YET</div>
        ) : (
          <div className="space-y-2">
            {outcomes.map((o) => (
              <div
                key={o.id}
                className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2"
                style={{ borderBottom: '1px solid rgba(0,212,255,0.08)' }}
              >
                {/* Verdict chip */}
                <span
                  className={`font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded ${verdictColor(o.verdict)}`}
                  style={{ border: `1px solid currentColor`, opacity: 0.9 }}
                >
                  {o.verdict}
                </span>

                {/* Confidence */}
                <span className="font-mono text-xs text-text-secondary">
                  {Math.round((o.confidence ?? 0) * 100)}%
                </span>

                {/* Grounded badge */}
                {o.grounded && (
                  <span
                    className="font-mono text-xs text-accent-cyan px-1.5 py-0.5 rounded"
                    style={{ border: '1px solid rgba(0,212,255,0.4)', fontSize: '0.6rem' }}
                  >
                    GROUNDED
                  </span>
                )}

                {/* Task ref */}
                <span className="font-mono text-xs text-text-secondary">
                  task #{o.task_id}
                </span>

                {/* Timestamp */}
                <span className="font-mono text-xs text-text-secondary ml-auto">
                  {relativeTime(o.created_at)}
                </span>

                {/* Reason — full width second line */}
                <div className="w-full font-mono text-xs text-text-primary opacity-80 pl-0.5 mt-0.5">
                  {o.reason}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Recent actions                                                       */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">RECENT ACTIONS</div>

      <div className="hud-panel-sm p-4">
        {actions === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : actions.length === 0 ? (
          <div className="hud-label opacity-40">NO ACTIONS LOGGED YET</div>
        ) : (
          <div className="space-y-1">
            {actions.map((a) => (
              <div
                key={a.id}
                className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5"
                style={{ borderBottom: '1px solid rgba(0,212,255,0.06)' }}
              >
                {/* Actor */}
                <span className="font-mono text-xs text-accent-cyan uppercase tracking-wide">
                  {a.actor}
                </span>
                <span className="text-text-secondary text-xs">›</span>

                {/* Kind */}
                <span className="font-mono text-xs text-text-primary">
                  {a.kind}
                </span>

                {/* Target */}
                {a.target && (
                  <span className="font-mono text-xs text-text-secondary truncate max-w-xs">
                    {a.target}
                  </span>
                )}

                {/* Decision chip */}
                <span
                  className={`font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded ml-auto ${decisionColor(a.decision)}`}
                  style={{ border: '1px solid currentColor', opacity: 0.85, fontSize: '0.6rem' }}
                >
                  {a.decision}
                </span>

                {/* Timestamp */}
                <span className="font-mono text-xs text-text-secondary">
                  {relativeTime(a.created_at)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
