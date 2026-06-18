import { useState, useEffect, useCallback, useRef } from 'react'
import { ShieldCheck } from 'lucide-react'
import { api, wsLogsUrl } from '../lib/api'

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
    case 'executed':      return 'text-accent-cyan'
    case 'needs_confirm': return 'text-accent-orange'
    default:              return 'text-red-400'
  }
}

function riskColor(risk) {
  switch ((risk || '').toLowerCase()) {
    case 'low':    return 'text-accent-cyan'
    case 'medium': return 'text-accent-orange'
    case 'high':   return 'text-red-400'
    default:       return 'text-text-secondary'
  }
}

function goalStatusColor(status) {
  switch ((status || '').toLowerCase()) {
    case 'proposed':  return 'text-accent-cyan'
    case 'approved':
    case 'running':   return 'text-accent-orange'
    case 'completed': return 'text-accent-cyan'
    case 'failed':    return 'text-red-400'
    default:          return 'text-text-secondary'
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
  const [status, setStatus]     = useState(null)
  const [outcomes, setOutcomes] = useState(null)
  const [actions, setActions]   = useState(null)
  const [toggling, setToggling] = useState(false)

  // Budget editor state
  const [dailyInput, setDailyInput]     = useState('')
  const [perTaskInput, setPerTaskInput] = useState('')
  const [budgetSaved, setBudgetSaved]   = useState(false)
  const [budgetErr, setBudgetErr]       = useState('')

  // New sections state
  const [events, setEvents]               = useState([])
  const [wsConnected, setWsConnected]     = useState(false)
  const [pendingActions, setPendingActions] = useState([])
  const [confirmingId, setConfirmingId]   = useState(null)
  const [confirmErrors, setConfirmErrors] = useState({})
  const [goals, setGoals]                 = useState([])
  const [goalActingId, setGoalActingId]   = useState(null)
  const [goalErrors, setGoalErrors]       = useState({})
  const [metering, setMetering]           = useState(null)

  // Goal propose form state
  const [proposeTitle, setProposeTitle]       = useState('')
  const [proposeDesc, setProposeDesc]         = useState('')
  const [proposeRisk, setProposeRisk]         = useState('medium')
  const [proposeCategory, setProposeCategory] = useState('other')
  const [proposeCadence, setProposeCadence]   = useState('')   // '' = one-shot
  const [proposeSuccess, setProposeSuccess]   = useState('')
  const [proposing, setProposing]             = useState(false)
  const [proposeErr, setProposeErr]           = useState('')

  // Goal category vocabulary + filter
  const FALLBACK_CATEGORIES = ["maintenance", "storage", "network", "media", "monitoring", "knowledge", "other"]
  const [categories, setCategories]       = useState(FALLBACK_CATEGORIES)
  const [categoryFilter, setCategoryFilter] = useState('all')

  // WebSocket refs (prevent stale closures / leak on unmount)
  const wsRef       = useRef(null)
  const wsAliveRef  = useRef(true)
  const reconnTimer = useRef(null)

  // ---------------------------------------------------------------------------
  // REST load (10s poll)
  // ---------------------------------------------------------------------------
  const load = useCallback(() => {
    api.safety.status().then(s => {
      setStatus(s)
      setDailyInput(v => v || (s.daily_budget_usd != null ? String(s.daily_budget_usd) : ''))
      setPerTaskInput(v => v || (s.per_task_budget_usd != null ? String(s.per_task_budget_usd) : ''))
    }).catch(() => {})
    api.safety.outcomes(20).then(setOutcomes).catch(() => {})
    api.safety.actions(20).then(setActions).catch(() => {})
    api.safety.pendingActions(20).then(setPendingActions).catch(() => {})
    api.goals.list().then(setGoals).catch(() => {})
    api.safety.metering().then(setMetering).catch(() => {})
  }, [])

  useEffect(() => {
    api.goals.categories().then(data => {
      if (data?.categories?.length) setCategories(data.categories)
    }).catch(() => { /* use fallback */ })
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

  // ---------------------------------------------------------------------------
  // WebSocket — live event feed
  // ---------------------------------------------------------------------------
  useEffect(() => {
    wsAliveRef.current = true

    function connect() {
      if (!wsAliveRef.current) return
      const ws = new WebSocket(wsLogsUrl())
      wsRef.current = ws

      ws.onopen = () => {
        if (wsAliveRef.current) setWsConnected(true)
      }

      ws.onmessage = (e) => {
        try {
          const evt = JSON.parse(e.data)
          setEvents(prev => [{ ...evt, _t: Date.now() }, ...prev].slice(0, 20))
        } catch {
          // ignore unparseable frames
        }
      }

      ws.onclose = () => {
        setWsConnected(false)
        if (wsAliveRef.current) {
          reconnTimer.current = setTimeout(connect, 3000)
        }
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()

    return () => {
      wsAliveRef.current = false
      clearTimeout(reconnTimer.current)
      if (wsRef.current) {
        wsRef.current.onclose = null  // prevent reconnect loop on intentional close
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [])

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------
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

  async function handleConfirm(id) {
    setConfirmingId(id)
    setConfirmErrors(prev => ({ ...prev, [id]: '' }))
    try {
      await api.safety.confirmAction(id)
      load()
    } catch (err) {
      const msg = err?.message || 'Failed to confirm action.'
      setConfirmErrors(prev => ({ ...prev, [id]: msg }))
    } finally {
      setConfirmingId(null)
    }
  }

  async function handleGoalApprove(id) {
    setGoalActingId(id)
    setGoalErrors(prev => ({ ...prev, [id]: '' }))
    try {
      await api.goals.approve(id)
      load()
    } catch (err) {
      const msg = err?.message || 'Failed to approve goal.'
      setGoalErrors(prev => ({ ...prev, [id]: msg }))
    } finally {
      setGoalActingId(null)
    }
  }

  async function handleGoalReject(id) {
    setGoalActingId(id)
    setGoalErrors(prev => ({ ...prev, [id]: '' }))
    try {
      await api.goals.reject(id)
      load()
    } catch (err) {
      const msg = err?.message || 'Failed to reject goal.'
      setGoalErrors(prev => ({ ...prev, [id]: msg }))
    } finally {
      setGoalActingId(null)
    }
  }

  async function handlePropose() {
    setProposeErr('')
    if (!proposeTitle.trim()) { setProposeErr('Title is required.'); return }
    if (!proposeDesc.trim())  { setProposeErr('Description is required.'); return }
    setProposing(true)
    try {
      await api.goals.propose(
        proposeTitle.trim(), proposeDesc.trim(), proposeRisk, proposeCategory,
        proposeCadence || null, proposeSuccess.trim() || null,
      )
      setProposeTitle('')
      setProposeDesc('')
      setProposeRisk('medium')
      setProposeCategory('other')
      setProposeCadence('')
      setProposeSuccess('')
      load()
    } catch (err) {
      setProposeErr(err?.message || 'Failed to propose goal.')
    } finally {
      setProposing(false)
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
      {/* Notify channel health                                                */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">NOTIFY CHANNEL</div>

      <div className="hud-panel-sm p-4 mb-6">
        {status === null || !status.notify_channel ? (
          <div className="hud-label opacity-40">NO NOTIFY CHANNEL DATA</div>
        ) : (() => {
          const nc = status.notify_channel
          const broken = nc.enabled && !nc.secret_present
          const healthy = nc.enabled && nc.secret_present && (nc.dead_lettered_count ?? 0) === 0
          return (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className={broken ? 'arc-dot-err' : healthy ? 'arc-dot' : 'arc-dot-warn'} />
                <span
                  className={`font-mono text-xs font-bold tracking-widest ${broken ? 'text-red-400' : healthy ? 'text-accent-cyan' : 'text-accent-orange'}`}
                >
                  {!nc.enabled ? 'NOTIFICATIONS DISABLED'
                    : broken ? 'SECRET MISSING — ALERTS FAILING'
                    : healthy ? 'HEALTHY'
                    : 'DELIVERIES STUCK'}
                </span>
              </div>
              {broken && (
                <div className="font-mono text-xs text-red-400 pl-5">
                  HERMES_WEBHOOK_SECRET is missing. Set it in Settings → Agent Bridge.
                </div>
              )}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1">
                <div>
                  <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>SECRET</div>
                  <div className={`font-mono text-sm ${nc.secret_present ? 'text-accent-cyan' : 'text-red-400'}`}>
                    {nc.secret_present ? 'SET' : 'MISSING'}
                  </div>
                </div>
                <div>
                  <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>PENDING</div>
                  <div className="font-mono text-sm text-text-primary">{nc.pending_count ?? 0}</div>
                </div>
                <div>
                  <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>DEAD-LETTERED</div>
                  <div className={`font-mono text-sm ${(nc.dead_lettered_count ?? 0) > 0 ? 'text-red-400' : 'text-text-secondary'}`}>
                    {nc.dead_lettered_count ?? 0}
                  </div>
                </div>
                <div>
                  <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>OLDEST (S)</div>
                  <div className="font-mono text-sm text-text-secondary">{nc.oldest_age_seconds ?? '—'}</div>
                </div>
              </div>
            </div>
          )
        })()}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Live event feed (WebSocket)                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3 flex items-center gap-3">
        LIVE ACTIVITY
        <span className={wsConnected ? 'arc-dot' : 'arc-dot-err'} />
        <span className="font-mono text-xs text-text-secondary">
          {wsConnected ? 'CONNECTED' : 'DISCONNECTED'}
        </span>
      </div>

      <div className="hud-panel-sm p-4 mb-6">
        {events.length === 0 ? (
          <div className="hud-label opacity-40">WAITING FOR ACTIVITY...</div>
        ) : (
          <div className="space-y-1">
            {events.map((evt, idx) => (
              <div
                key={`${evt._t}-${idx}`}
                className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5"
                style={{ borderBottom: '1px solid rgba(0,212,255,0.06)' }}
              >
                {evt.type === 'action' ? (
                  <>
                    <span
                      className={`font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded ${decisionColor(evt.decision)}`}
                      style={{ border: '1px solid currentColor', opacity: 0.85, fontSize: '0.6rem' }}
                    >
                      {evt.decision}
                    </span>
                    <span className="font-mono text-xs text-accent-cyan uppercase">{evt.actor}</span>
                    <span className="text-text-secondary text-xs">›</span>
                    <span className="font-mono text-xs text-text-primary">{evt.kind}</span>
                    {evt.target && (
                      <span className="font-mono text-xs text-text-secondary truncate max-w-xs">{evt.target}</span>
                    )}
                    <span className="font-mono text-xs text-text-secondary ml-auto">
                      {relativeTime(new Date(evt._t).toISOString())}
                    </span>
                  </>
                ) : evt.type === 'autonomy' ? (
                  <>
                    <span className={`font-mono text-xs font-bold tracking-widest ${evt.enabled ? 'text-accent-cyan' : 'text-red-400'}`}>
                      AUTONOMY {evt.enabled ? 'ENABLED' : 'PAUSED'}
                    </span>
                    <span className="font-mono text-xs text-text-secondary ml-auto">
                      {relativeTime(new Date(evt._t).toISOString())}
                    </span>
                  </>
                ) : (
                  <span className="font-mono text-xs text-text-secondary">
                    {JSON.stringify(evt)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Pending confirmations                                                */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-orange pl-2 mb-3">PENDING CONFIRMATIONS</div>

      <div className="hud-panel-sm p-4 mb-6">
        {pendingActions.length === 0 ? (
          <div className="hud-label opacity-40">NO ACTIONS AWAITING CONFIRMATION</div>
        ) : (
          <div className="space-y-2">
            {pendingActions.map((a) => (
              <div
                key={a.id}
                className="py-3"
                style={{ borderBottom: '1px solid rgba(0,212,255,0.08)' }}
              >
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-2">
                  {/* Risk chip */}
                  <span
                    className={`font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded ${riskColor(a.risk)}`}
                    style={{ border: '1px solid currentColor', opacity: 0.9, fontSize: '0.6rem' }}
                  >
                    {a.risk || 'RISK?'}
                  </span>
                  {/* Actor */}
                  <span className="font-mono text-xs text-accent-cyan uppercase">{a.actor}</span>
                  <span className="text-text-secondary text-xs">›</span>
                  {/* Kind */}
                  <span className="font-mono text-xs text-text-primary">{a.kind}</span>
                  {/* Target */}
                  {a.target && (
                    <span className="font-mono text-xs text-text-secondary truncate max-w-xs">{a.target}</span>
                  )}
                  <span className="font-mono text-xs text-text-secondary ml-auto">
                    {relativeTime(a.created_at)}
                  </span>
                </div>
                <div className="flex items-center gap-3 flex-wrap">
                  <button
                    onClick={() => handleConfirm(a.id)}
                    disabled={confirmingId === a.id}
                    className="glow-btn-gold px-4 py-1.5 text-xs tracking-widest disabled:opacity-40"
                  >
                    {confirmingId === a.id ? 'CONFIRMING...' : 'CONFIRM'}
                  </button>
                  {confirmErrors[a.id] && (
                    <span className="font-mono text-xs text-red-400">{confirmErrors[a.id]}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Goals                                                                */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-1">GOALS</div>
      <div className="font-mono text-xs text-text-secondary mb-3 pl-3">
        Approving a goal dispatches a task you authorized — autonomy stays off; nothing self-proposes.
      </div>

      <div className="hud-panel-sm p-4 mb-6">
        {/* Propose form */}
        <div className="mb-4 pb-4" style={{ borderBottom: '1px solid rgba(0,212,255,0.12)' }}>
          <div className="hud-label mb-3">PROPOSE NEW GOAL</div>
          <div className="space-y-2">
            <div>
              <label className="hud-label mb-1 block">TITLE</label>
              <input
                type="text"
                value={proposeTitle}
                onChange={e => { setProposeTitle(e.target.value); setProposeErr('') }}
                placeholder="Short goal title"
                className="hud-input w-full font-mono"
              />
            </div>
            <div>
              <label className="hud-label mb-1 block">DESCRIPTION</label>
              <textarea
                value={proposeDesc}
                onChange={e => { setProposeDesc(e.target.value); setProposeErr('') }}
                placeholder="Describe the goal in detail"
                rows={3}
                className="hud-input w-full font-mono resize-y"
              />
            </div>
            <div className="flex gap-3 flex-wrap">
              <div>
                <label className="hud-label mb-1 block">RISK</label>
                <select
                  value={proposeRisk}
                  onChange={e => setProposeRisk(e.target.value)}
                  className="hud-input font-mono"
                >
                  <option value="low">LOW</option>
                  <option value="medium">MEDIUM</option>
                  <option value="high">HIGH</option>
                </select>
              </div>
              <div>
                <label className="hud-label mb-1 block">CATEGORY</label>
                <select
                  value={proposeCategory}
                  onChange={e => setProposeCategory(e.target.value)}
                  className="hud-input font-mono"
                >
                  {categories.map(c => (
                    <option key={c} value={c}>{c.toUpperCase()}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="hud-label mb-1 block">CADENCE</label>
                <select
                  value={proposeCadence}
                  onChange={e => setProposeCadence(e.target.value)}
                  className="hud-input font-mono"
                >
                  <option value="">ONE-SHOT</option>
                  <option value="daily">DAILY</option>
                  <option value="weekly">WEEKLY</option>
                  <option value="monthly">MONTHLY</option>
                </select>
              </div>
            </div>
            <div>
              <label className="hud-label mb-1 block">
                SUCCESS CRITERIA <span className="text-text-secondary">(optional, for recurring goals)</span>
              </label>
              <input
                type="text"
                value={proposeSuccess}
                onChange={e => setProposeSuccess(e.target.value)}
                placeholder="A measurable check, e.g. 'Unraid usage < 85%'"
                className="hud-input w-full font-mono"
              />
            </div>
            <div className="flex items-center gap-3 flex-wrap pt-1">
              <button
                onClick={handlePropose}
                disabled={proposing}
                className="glow-btn px-4 py-2 text-xs tracking-widest disabled:opacity-40"
                style={{ boxShadow: '0 0 10px rgba(0,212,255,0.35)' }}
              >
                {proposing ? 'PROPOSING...' : 'PROPOSE GOAL'}
              </button>
              {proposeErr && (
                <span className="font-mono text-xs text-red-400">{proposeErr}</span>
              )}
            </div>
          </div>
        </div>

        {/* Category filter */}
        <div className="mb-3 flex items-center gap-2">
          <label className="hud-label">FILTER BY CATEGORY:</label>
          <select
            value={categoryFilter}
            onChange={e => setCategoryFilter(e.target.value)}
            className="hud-input font-mono"
          >
            <option value="all">ALL</option>
            {categories.map(c => (
              <option key={c} value={c}>{c.toUpperCase()}</option>
            ))}
          </select>
        </div>

        {/* Goals list */}
        {goals.length === 0 ? (
          <div className="hud-label opacity-40">NO GOALS YET</div>
        ) : (
          <div className="space-y-2">
            {goals.filter(g => categoryFilter === 'all' || g.category === categoryFilter).map((g) => (
              <div
                key={g.id}
                className="py-3"
                style={{ borderBottom: '1px solid rgba(0,212,255,0.08)' }}
              >
                <div className="flex flex-wrap items-start gap-x-2 gap-y-1 mb-2">
                  {/* Status chip */}
                  <span
                    className={`font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded ${goalStatusColor(g.status)}`}
                    style={{ border: '1px solid currentColor', opacity: 0.9, fontSize: '0.6rem' }}
                  >
                    {g.status}
                  </span>
                  {/* Risk chip */}
                  <span
                    className={`font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded ${riskColor(g.risk)}`}
                    style={{ border: '1px solid currentColor', opacity: 0.85, fontSize: '0.6rem' }}
                  >
                    {g.risk || 'MEDIUM'}
                  </span>
                  {/* Category chip */}
                  {g.category && (
                    <span
                      className="font-mono text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded text-accent-cyan"
                      style={{ border: '1px solid rgba(0,212,255,0.45)', opacity: 0.8, fontSize: '0.6rem' }}
                    >
                      {g.category}
                    </span>
                  )}
                  {/* Title */}
                  <span className="font-mono text-xs text-text-primary flex-1 min-w-0">{g.title}</span>
                  {/* Time */}
                  <span className="font-mono text-xs text-text-secondary">
                    {relativeTime(g.created_at)}
                  </span>
                </div>

                {/* Running: show task_id */}
                {(g.status === 'running' || g.status === 'approved') && g.task_id && (
                  <div className="font-mono text-xs text-accent-orange mb-2">
                    TASK #{g.task_id}
                  </div>
                )}

                {/* Failed: explain WHY (verify_rejected reason etc.) */}
                {g.status === 'failed' && g.rejection_reason && (
                  <div className="font-mono text-xs text-red-400 mb-2 opacity-90">
                    {g.rejection_reason}
                  </div>
                )}

                {/* Proposed: approve/reject */}
                {g.status === 'proposed' && (
                  <div className="flex items-center gap-3 flex-wrap">
                    <button
                      onClick={() => handleGoalApprove(g.id)}
                      disabled={goalActingId === g.id}
                      className="glow-btn px-4 py-1.5 text-xs tracking-widest disabled:opacity-40"
                      style={{ boxShadow: '0 0 10px rgba(0,212,255,0.35)' }}
                    >
                      {goalActingId === g.id ? 'APPROVING...' : 'APPROVE'}
                    </button>
                    <button
                      onClick={() => handleGoalReject(g.id)}
                      disabled={goalActingId === g.id}
                      className="font-mono text-xs text-text-secondary px-3 py-1.5 rounded disabled:opacity-40"
                      style={{ border: '1px solid rgba(255,255,255,0.12)' }}
                    >
                      {goalActingId === g.id ? 'REJECTING...' : 'REJECT'}
                    </button>
                    {goalErrors[g.id] && (
                      <span className="font-mono text-xs text-red-400">{goalErrors[g.id]}</span>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Metering health                                                      */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">METERING HEALTH</div>

      <div className="hud-panel-sm p-4 mb-6">
        {metering === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : (
          <div className="space-y-3">
            {/* Prices verified badge */}
            <div className="flex items-center gap-2">
              <span className={metering.prices_verified ? 'arc-dot' : 'arc-dot-warn'} />
              <span
                className={`font-mono text-xs font-bold tracking-widest ${metering.prices_verified ? 'text-accent-cyan' : 'text-accent-orange'}`}
              >
                {metering.prices_verified ? 'PRICES VERIFIED' : 'PRICES UNVERIFIED'}
              </span>
            </div>
            {!metering.prices_verified && (
              <div className="font-mono text-xs text-text-secondary pl-5">
                Cost caps may be inaccurate until verified.
              </div>
            )}

            {/* Today stats */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1">
              <div>
                <div className="hud-label mb-1">TODAY SPEND</div>
                <div className="font-mono text-sm text-accent-cyan">{fmtUsd(metering.today_spend_usd)}</div>
              </div>
              <div>
                <div className="hud-label mb-1">ROWS TODAY</div>
                <div className="font-mono text-sm text-text-primary">{metering.today_row_count ?? 0}</div>
              </div>
            </div>

            {/* Counters */}
            {metering.counters && (
              <div className="pt-1">
                <div className="hud-label mb-2">SPEND LOG COUNTERS</div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  <div>
                    <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>RECORDED</div>
                    <div className="font-mono text-sm text-accent-cyan">{metering.counters.recorded ?? 0}</div>
                  </div>
                  <div>
                    <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>SKIPPED (NO USAGE)</div>
                    <div className="font-mono text-sm text-text-secondary">{metering.counters.skipped_no_usage ?? 0}</div>
                  </div>
                  <div>
                    <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>SKIPPED (UNPARSEABLE)</div>
                    <div className="font-mono text-sm text-accent-orange">{metering.counters.skipped_unparseable ?? 0}</div>
                  </div>
                  <div>
                    <div className="hud-label mb-1" style={{ fontSize: '0.6rem' }}>FAILED</div>
                    <div className="font-mono text-sm text-red-400">{metering.counters.failed ?? 0}</div>
                  </div>
                </div>
              </div>
            )}
          </div>
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
