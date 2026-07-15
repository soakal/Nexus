import { useState, useEffect, useCallback, useRef } from 'react'
import { api, wsLogsUrl, wsLogsProtocols } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'

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

// ---------------------------------------------------------------------------
// Tone + Badge helpers
// ---------------------------------------------------------------------------

const tone = (s) => {
  if (!s) return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  const u = s.toLowerCase()
  if (u.includes('executed') || u.includes('allowed') || u.includes('success'))
    return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (u.includes('confirm') || u.includes('warn') || u.includes('partial'))
    return { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }
  return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
}

const toneRisk = (r) => {
  if (!r) return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  const u = r.toLowerCase()
  if (u === 'low') return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (u === 'medium') return { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }
  return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
}

const toneStatus = (s) => {
  if (!s) return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  const u = s.toLowerCase()
  if (u === 'completed' || u === 'approved') return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (u === 'running' || u === 'proposed') return { c: '#2fd4ee', bg: 'rgba(47,212,238,0.08)', bd: 'rgba(47,212,238,0.30)' }
  if (u === 'failed') return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
  return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
}

// Days between now and an ISO timestamp. NEXUS stamps these as naive UTC
// (datetime.utcnow().isoformat(), no trailing 'Z') — parse with the same
// '+Z' convention already used for ActionLog timestamps above, or a bare
// new Date(iso) would be read as local time and skew the age by the UTC offset.
function daysSince(iso) {
  if (!iso) return null
  const t = new Date(iso.endsWith('Z') ? iso : iso + 'Z').getTime()
  if (Number.isNaN(t)) return null
  return Math.floor((Date.now() - t) / 86400000)
}

const toneStaleness = (days) => {
  if (days === null) return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  if (days < 90) return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (days <= 180) return { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }
  return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
}

// Collapse `keys` + `meta` (from GET /secrets/list) into one row per secret,
// grouping cred:<service>:<field> entries (each field stamped separately by
// set_credential) into a single row per service dated by its newest field.
// A key with no meta entry (never actually set_secret'd) is its own "unknown"
// state — never fabricated as "0 days", and excluded from the stalest ranking.
function buildSecretRows(keys, meta) {
  const top = []
  const credGroups = {}
  for (const key of keys || []) {
    const m = (meta || {})[key]
    if (key.startsWith('cred:')) {
      const parts = key.split(':')
      const service = parts[1] || key
      const days = m ? daysSince(m.last_rotated || m.last_set) : null
      const g = credGroups[service] || { name: service, days: null }
      if (days !== null && (g.days === null || days < g.days)) g.days = days
      credGroups[service] = g
    } else {
      top.push({ name: key, days: m ? daysSince(m.last_rotated || m.last_set) : null })
    }
  }
  return [...top, ...Object.values(credGroups)]
}

const Badge = ({ label, t }) => (
  <span style={{
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '3px 8px', borderRadius: '6px',
    background: t.bg, border: `1px solid ${t.bd}`,
    fontSize: '10px', fontWeight: 700, letterSpacing: '0.08em', color: t.c,
    textTransform: 'uppercase', whiteSpace: 'nowrap',
  }}>{label}</span>
)

// ---------------------------------------------------------------------------
// SpendBar
// ---------------------------------------------------------------------------

function SpendBar({ spend, budget }) {
  const pct = budget > 0 ? Math.min(100, (spend / budget) * 100) : 0
  const barColor = pct >= 100 ? '#fb7185' : pct >= 80 ? '#fbbf24' : '#2fd4ee'
  return (
    <div style={{ marginTop: '8px' }}>
      <div style={{
        position: 'relative', height: '6px', borderRadius: '999px',
        overflow: 'hidden', background: 'rgba(255,255,255,0.06)',
        border: '1px solid rgba(120,160,220,0.12)',
      }}>
        <div style={{
          height: '100%', borderRadius: '999px',
          width: `${pct}%`,
          background: `linear-gradient(90deg, ${barColor}99, ${barColor})`,
          boxShadow: `0 0 8px ${barColor}66`,
          transition: 'width 0.5s ease',
        }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '6px' }}>
        <span style={{ fontSize: '12px', color: '#8a96ad', fontFamily: "'JetBrains Mono', monospace" }}>
          {fmtUsd(spend)} / {fmtUsd(budget)}
        </span>
        <span style={{ fontSize: '12px', fontFamily: "'JetBrains Mono', monospace", color: barColor }}>
          {fmtPct(pct)}
        </span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Inline styles for form fields
// ---------------------------------------------------------------------------

const inputStyle = {
  background: 'rgba(255,255,255,0.03)',
  color: '#e9eef8',
  border: '1px solid rgba(120,160,220,0.16)',
  borderRadius: '10px',
  padding: '10px 12px',
  fontSize: '13px',
  outline: 'none',
  width: '100%',
  boxSizing: 'border-box',
  fontFamily: "'Space Grotesk', sans-serif",
}

const selectStyle = {
  ...inputStyle,
  cursor: 'pointer',
  appearance: 'none',
  WebkitAppearance: 'none',
}

const labelStyle = {
  fontSize: '10px',
  textTransform: 'uppercase',
  letterSpacing: '0.1em',
  color: '#5d6982',
  fontWeight: 600,
  marginBottom: '4px',
  display: 'block',
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Safety() {
  const [status, setStatus]     = useState(null)
  const [outcomes, setOutcomes] = useState(null)
  const [actions, setActions]   = useState(null)
  const [toggling, setToggling] = useState(false)

  // Dead-letter clear
  const [clearing, setClearing] = useState(false)
  const clearDeadLetters = async () => {
    setClearing(true)
    try { await api.safety.clearDeadLetters() } catch (_) {}
    setClearing(false)
    load()
  }

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

  // Inline goal edit state
  const [editingGoalId, setEditingGoalId] = useState(null)
  const [editFields, setEditFields]       = useState({ title: '', description: '', risk: 'medium', category: 'other' })
  const [metering, setMetering]           = useState(null)
  const [secretsMeta, setSecretsMeta]     = useState(null)
  const [showAllSecrets, setShowAllSecrets] = useState(false)
  const [hermesVerbs, setHermesVerbs]     = useState(null)

  // Goal propose form state
  const [proposeTitle, setProposeTitle]       = useState('')
  const [proposeDesc, setProposeDesc]         = useState('')
  const [proposeRisk, setProposeRisk]         = useState('medium')
  const [proposeCategory, setProposeCategory] = useState('other')
  const [proposeCadence, setProposeCadence]   = useState('')
  const [proposeSuccess, setProposeSuccess]   = useState('')
  const [proposing, setProposing]             = useState(false)
  const [proposeErr, setProposeErr]           = useState('')

  // Goal category vocabulary + filter
  const FALLBACK_CATEGORIES = ["maintenance", "storage", "network", "media", "monitoring", "knowledge", "other"]
  const [categories, setCategories]       = useState(FALLBACK_CATEGORIES)
  const [categoryFilter, setCategoryFilter] = useState('all')

  // WebSocket refs
  const wsRef       = useRef(null)
  const wsAliveRef  = useRef(true)
  const reconnTimer = useRef(null)
  const backfilledRef = useRef(false)

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
    }).catch(() => {})
  }, [])

  // Secret rotation staleness — fetched once on mount, not on the 10s poll:
  // rotation dates don't change mid-session, so there's nothing to keep polling.
  useEffect(() => {
    api.secrets.list().then(setSecretsMeta).catch(() => {})
  }, [])

  // Hermes capabilities — the allowlist doesn't change mid-session either.
  useEffect(() => {
    api.safety.hermesActions().then(setHermesVerbs).catch(() => {})
  }, [])

  useEffect(() => {
    if (backfilledRef.current || !actions || actions.length === 0) return
    backfilledRef.current = true
    const seeded = actions.slice(0, 15).map(a => ({
      type: 'action',
      actor: a.actor,
      kind: a.kind,
      target: a.target,
      decision: a.decision,
      _t: a.created_at ? new Date(a.created_at.endsWith('Z') ? a.created_at : a.created_at + 'Z').getTime() : Date.now(),
      _backfill: true,
    }))
    setEvents(prev => (prev.length === 0 ? seeded : prev))
  }, [actions])

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
      const ws = new WebSocket(wsLogsUrl(), wsLogsProtocols())
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
        wsRef.current.onclose = null
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
      // swallow
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
      const raw = err?.message || ''
      const msg = raw.startsWith('410') ? 'Approval window expired — re-run the goal to request a new one.'
                : raw.startsWith('403') ? 'Autonomy is paused. Resume autonomy first, then confirm.'
                : raw.startsWith('409') ? 'This action is no longer awaiting confirmation.'
                : raw || 'Failed to confirm action.'
      setConfirmErrors(prev => ({ ...prev, [id]: msg }))
      load()
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

  async function handleGoalDelete(id) {
    if (!window.confirm('Delete this goal permanently? This cannot be undone.')) return
    setGoalActingId(id)
    setGoalErrors(prev => ({ ...prev, [id]: '' }))
    try {
      await api.goals.remove(id)
      load()
    } catch (err) {
      const msg = err?.message || 'Failed to delete goal.'
      setGoalErrors(prev => ({ ...prev, [id]: msg }))
    } finally {
      setGoalActingId(null)
    }
  }

  async function handleGoalToggleDisabled(g) {
    setGoalActingId(g.id)
    setGoalErrors(prev => ({ ...prev, [g.id]: '' }))
    try {
      if (g.disabled) {
        await api.goals.enable(g.id)
      } else {
        await api.goals.disable(g.id)
      }
      load()
    } catch (err) {
      const msg = err?.message || 'Failed to update goal.'
      setGoalErrors(prev => ({ ...prev, [g.id]: msg }))
    } finally {
      setGoalActingId(null)
    }
  }

  function handleStartEdit(g) {
    setEditingGoalId(g.id)
    setEditFields({
      title: g.title || '',
      description: g.description || '',
      risk: g.risk || 'medium',
      category: g.category || 'other',
    })
    setGoalErrors(prev => ({ ...prev, [g.id]: '' }))
  }

  function handleCancelEdit() {
    setEditingGoalId(null)
  }

  async function handleSaveEdit(id) {
    if (!editFields.title.trim() || !editFields.description.trim()) {
      setGoalErrors(prev => ({ ...prev, [id]: 'Title and description are required.' }))
      return
    }
    setGoalActingId(id)
    setGoalErrors(prev => ({ ...prev, [id]: '' }))
    try {
      await api.goals.edit(id, {
        title: editFields.title.trim(),
        description: editFields.description.trim(),
        risk: editFields.risk,
        category: editFields.category,
      })
      setEditingGoalId(null)
      load()
    } catch (err) {
      const msg = err?.message || 'Failed to save goal.'
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

  // ---------------------------------------------------------------------------
  // Row style for activity / verdict items
  // ---------------------------------------------------------------------------
  // flexWrap is load-bearing: without it, a narrow (phone) viewport squeezes the
  // flex:1 target span to zero width — badges + actor/kind eat the whole row and
  // the target/reason become invisible. Rows must wrap, never clip.
  const rowStyle = {
    display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: '12px',
    padding: '11px 14px', borderRadius: '11px',
    background: 'rgba(255,255,255,0.022)',
    border: '1px solid rgba(120,160,220,0.08)',
    marginBottom: '6px',
  }

  return (
    <div style={{
      width: '100%', maxWidth: '1100px', margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex', flexDirection: 'column', gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Safety" title="Safety & Governance" />

      {/* ------------------------------------------------------------------ */}
      {/* 1. Autonomy Control                                                  */}
      {/* ------------------------------------------------------------------ */}
      <Card accent="cyan">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '14px' }}>
          {/* Left: shield + label */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
              stroke={autonomyOn ? '#34d399' : '#fb7185'}
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              style={{ filter: autonomyOn ? 'drop-shadow(0 0 5px rgba(52,211,153,0.5))' : 'drop-shadow(0 0 5px rgba(251,113,133,0.5))' }}
            >
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
            <span style={{ fontSize: '15px', fontWeight: 600, color: autonomyOn ? '#5fe0b4' : '#fb7185' }}>
              {autonomyOn ? 'Autonomy enabled' : 'Autonomy disabled'}
            </span>
            {autonomyOn !== null && (
              <StatusDot
                color={autonomyOn ? '#34d399' : '#fb7185'}
                size={8}
                pulse={autonomyOn}
                glow
              />
            )}
          </div>

          {/* Right: action buttons */}
          {status !== null && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              {autonomyOn ? (
                <button
                  onClick={handleToggle}
                  disabled={toggling}
                  style={{
                    border: '1px solid rgba(251,191,36,0.4)',
                    background: 'rgba(251,191,36,0.08)',
                    color: '#fbbf24',
                    padding: '9px 16px',
                    borderRadius: '10px',
                    fontWeight: 700,
                    fontSize: '13px',
                    cursor: toggling ? 'not-allowed' : 'pointer',
                    opacity: toggling ? 0.5 : 1,
                    fontFamily: 'inherit',
                  }}
                >
                  {toggling ? 'Pausing...' : 'Pause / Kill Switch'}
                </button>
              ) : (
                <PrimaryButton onClick={handleToggle} disabled={toggling}>
                  {toggling ? 'Resuming...' : 'Resume Autonomy'}
                </PrimaryButton>
              )}
            </div>
          )}
        </div>

        {status && (
          <div style={{ marginTop: '10px', fontSize: '12px', color: '#5d6982' }}>
            Scheduler {status.scheduler_running ? 'running' : 'stopped'}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 2. Notify Channel / Deliveries                                       */}
      {/* ------------------------------------------------------------------ */}
      <Card accent="amber">
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Deliveries</Eyebrow>
        {status === null || !status.notify_channel ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No notify channel data</span>
        ) : (() => {
          const nc = status.notify_channel
          const broken = nc.enabled && !nc.secret_present
          const healthy = nc.enabled && nc.secret_present && (nc.dead_lettered_count ?? 0) === 0
          const pendingCount = nc.pending_count ?? 0
          const deadCount = nc.dead_lettered_count ?? 0
          const oldestSec = nc.oldest_age_seconds ?? null
          return (
            <>
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(70px, 1fr))',
                gap: '14px',
                marginBottom: '14px',
              }}>
                {/* Secret */}
                <div>
                  <div style={labelStyle}>Secret</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: nc.secret_present ? 'var(--accent)' : '#fb7185' }}>
                    {nc.secret_present ? 'Set' : 'Missing'}
                  </div>
                </div>
                {/* Pending */}
                <div>
                  <div style={labelStyle}>Pending</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: pendingCount > 0 ? '#fbbf24' : '#e9eef8' }}>
                    {pendingCount}
                  </div>
                </div>
                {/* Dead-lettered */}
                <div>
                  <div style={labelStyle}>Dead-lettered</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: deadCount > 0 ? '#fb7185' : '#e9eef8' }}>
                    {deadCount}
                  </div>
                </div>
                {/* Oldest */}
                <div>
                  <div style={labelStyle}>Oldest (s)</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: (oldestSec != null && oldestSec > 10) ? '#fbbf24' : '#e9eef8' }}>
                    {oldestSec ?? '—'}
                  </div>
                </div>
              </div>

              {/* Status line */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <StatusDot
                    color={broken ? '#fb7185' : healthy ? '#34d399' : '#fbbf24'}
                    size={7}
                    glow
                  />
                  <span style={{
                    fontSize: '12px', fontWeight: 600,
                    color: broken ? '#fb7185' : healthy ? '#5fe0b4' : '#f4d27a',
                  }}>
                    {!nc.enabled ? 'Notifications disabled'
                      : broken ? 'Secret missing — alerts failing'
                      : healthy ? 'Healthy'
                      : 'Deliveries stuck'}
                  </span>
                </div>
                {deadCount > 0 && (
                  <button onClick={clearDeadLetters} disabled={clearing} style={{
                    fontSize: '11px', fontWeight: 700, padding: '4px 10px', borderRadius: '7px',
                    border: '1px solid rgba(251,113,133,0.35)', background: 'rgba(251,113,133,0.08)',
                    color: '#fb7185', cursor: clearing ? 'not-allowed' : 'pointer', opacity: clearing ? 0.6 : 1,
                  }}>
                    {clearing ? 'Clearing…' : `Clear ${deadCount} stuck`}
                  </button>
                )}
              </div>
            </>
          )
        })()}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 2b. Secret Rotation                                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Secret Rotation</Eyebrow>
        {secretsMeta === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : (() => {
          const rows = buildSecretRows(secretsMeta.keys, secretsMeta.meta)
          const known = rows.filter(r => r.days !== null)
          const unknownCount = rows.length - known.length
          const stalest = known.length
            ? known.reduce((a, b) => (b.days > a.days ? b : a))
            : null
          const t = stalest ? toneStaleness(stalest.days) : toneStaleness(null)
          return (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: unknownCount ? '6px' : '0' }}>
                <StatusDot color={t.c} size={7} glow />
                <span style={{ fontSize: '13px', fontWeight: 600, color: t.c }}>
                  {stalest
                    ? `Stalest secret: ${stalest.name}, rotated ${stalest.days}d ago`
                    : 'No rotation history yet'}
                </span>
              </div>
              {unknownCount > 0 && (
                <div style={{ fontSize: '11px', color: '#5d6982', marginBottom: '6px' }}>
                  {unknownCount} secret{unknownCount === 1 ? '' : 's'} never stamped
                </div>
              )}
              <button
                onClick={() => setShowAllSecrets(v => !v)}
                style={{
                  fontSize: '11px', fontWeight: 600, color: '#5d6982', background: 'none',
                  border: 'none', cursor: 'pointer', padding: 0, marginTop: '4px',
                }}
              >
                {showAllSecrets ? 'Hide' : `Show all ${rows.length}`}
              </button>
              {showAllSecrets && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '10px' }}>
                  {rows.map(r => (
                    <div key={r.name} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px' }}>
                      <span style={{ color: '#8a96ad' }}>{r.name}</span>
                      <Badge label={r.days === null ? 'unknown' : `${r.days}d ago`} t={toneStaleness(r.days)} />
                    </div>
                  ))}
                </div>
              )}
            </>
          )
        })()}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 2c. Hermes Capabilities                                               */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Hermes Capabilities</Eyebrow>
        {hermesVerbs === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : hermesVerbs.verbs.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No verbs configured</span>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {hermesVerbs.verbs.map(v => (
              <div key={v.verb} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  <span style={{ fontSize: '13px', color: '#e9eef8', fontFamily: "'JetBrains Mono', monospace" }}>{v.verb}</span>
                  {(v.required_args.length > 0 || Object.keys(v.enum_args).length > 0) && (
                    <span style={{ fontSize: '11px', color: '#5d6982' }}>
                      {v.required_args.join(', ')}
                      {Object.entries(v.enum_args).map(([k, vals]) => ` ${k}: ${vals.join('|')}`).join(', ')}
                    </span>
                  )}
                </div>
                <Badge label={v.risk} t={toneRisk(v.risk)} />
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 3. Live Activity                                                      */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
          <Eyebrow>Live Activity</Eyebrow>
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
            <StatusDot color={wsConnected ? '#34d399' : '#8a96ad'} size={7} glow={wsConnected} />
            <span style={{ fontSize: '12px', color: wsConnected ? '#5fe0b4' : '#8a96ad' }}>
              {wsConnected ? 'Connected' : 'Disconnected'}
            </span>
          </div>
        </div>

        {events.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Waiting for activity...</span>
        ) : (
          <div>
            {events.map((evt, idx) => (
              <div key={`${evt._t}-${idx}`} style={rowStyle}>
                {evt.type === 'action' ? (
                  <>
                    <Badge label={evt.decision || 'event'} t={tone(evt.decision)} />
                    <span style={{ fontSize: '12px', color: '#8a96ad' }}>
                      {[evt.actor, evt.kind].filter(Boolean).join(' / ')}
                    </span>
                    <span style={{ marginLeft: 'auto', fontSize: '11px', color: '#5d6982', flex: 'none' }}>
                      {relativeTime(new Date(evt._t).toISOString())}
                    </span>
                    {evt.target && (
                      <span style={{
                        width: '100%', fontSize: '13px', color: '#dbe3f0',
                        fontFamily: "'JetBrains Mono', monospace",
                        overflowWrap: 'anywhere', lineHeight: 1.45,
                      }}>
                        {evt.target}
                      </span>
                    )}
                  </>
                ) : evt.type === 'autonomy' ? (
                  <>
                    <Badge label={evt.enabled ? 'autonomy on' : 'autonomy off'} t={tone(evt.enabled ? 'allowed' : 'denied')} />
                    <span style={{ flex: 1 }} />
                    <span style={{ fontSize: '11px', color: '#5d6982', flex: 'none' }}>
                      {relativeTime(new Date(evt._t).toISOString())}
                    </span>
                  </>
                ) : (
                  <span style={{ fontSize: '12px', color: '#5d6982', fontFamily: "'JetBrains Mono', monospace" }}>
                    {JSON.stringify(evt)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 4. Pending Confirmations (only if any)                               */}
      {/* ------------------------------------------------------------------ */}
      {pendingActions?.length > 0 && (
        <Card>
          <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Pending Confirmations</Eyebrow>
          <div>
            {pendingActions.map((a) => (
              <div key={a.id} style={{ ...rowStyle, flexWrap: 'wrap', alignItems: 'flex-start', gap: '10px', marginBottom: '8px' }}>
                <Badge label={a.risk || 'risk?'} t={toneRisk(a.risk)} />
                <span style={{ fontSize: '12px', color: '#8a96ad' }}>
                  {[a.actor, a.kind].filter(Boolean).join(' / ')}
                </span>
                {a.target && (
                  <span style={{
                    flex: '1 1 160px', minWidth: 0, fontSize: '13px', color: '#dbe3f0',
                    fontFamily: "'JetBrains Mono', monospace",
                    overflowWrap: 'anywhere',
                  }}>
                    {a.target}
                  </span>
                )}
                <span style={{ fontSize: '11px', color: '#5d6982', flex: 'none' }}>
                  {relativeTime(a.created_at)}
                </span>
                {a.judge_reason && (
                  <div style={{ width: '100%', fontSize: '13px', color: '#aab4c7', lineHeight: 1.55, overflowWrap: 'anywhere' }}>
                    Judge: {a.judge_reason}
                  </div>
                )}
                <div style={{ width: '100%', display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                  <button
                    onClick={() => handleConfirm(a.id)}
                    disabled={confirmingId === a.id}
                    style={{
                      border: '1px solid rgba(251,191,36,0.3)',
                      background: 'rgba(251,191,36,0.06)',
                      color: '#fbbf24',
                      padding: '7px 14px',
                      borderRadius: '8px',
                      fontWeight: 700,
                      fontSize: '12px',
                      cursor: confirmingId === a.id ? 'not-allowed' : 'pointer',
                      opacity: confirmingId === a.id ? 0.5 : 1,
                      fontFamily: 'inherit',
                    }}
                  >
                    {confirmingId === a.id ? 'Confirming...' : 'Confirm'}
                  </button>
                  {confirmErrors[a.id] && (
                    <span style={{ fontSize: '12px', color: '#fb7185' }}>{confirmErrors[a.id]}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* 5. Goals                                                             */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Goals</Eyebrow>

        {/* Health tile — surfaces a re-proposal/spam loop (like the one just fixed)
            at a glance next time, instead of by Brian noticing repeated messages.
            Scoped to updated_at within 7d: a one-shot goal that fails has no path
            back to completed/abandoned (only cadence-recurring goals re-eligibilize),
            so an unscoped count would only ever grow, never reflect current health. */}
        {(() => {
          const weekMs = 7 * 86400000
          const failedThisWeek = goals.filter(g => {
            if (g.status !== 'failed' || !g.updated_at) return false
            const t = new Date(g.updated_at.endsWith('Z') ? g.updated_at : g.updated_at + 'Z').getTime()
            return !Number.isNaN(t) && (Date.now() - t) < weekMs
          })
          // A blank fingerprint ("" is the Goal model default) must never count as
          // a shared identity — otherwise unrelated goals that happen to both lack
          // a fingerprint would falsely collapse into "1 recurring goal failing".
          const fps = new Set(failedThisWeek.map(g => g.fingerprint))
          const distinctFps = fps.size
          const healthy = failedThisWeek.length === 0
          const looping = distinctFps === 1 && failedThisWeek.length > 1 && !fps.has('')
          const t = healthy
            ? { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
            : { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '14px' }}>
              <StatusDot color={t.c} size={7} glow />
              <span style={{ fontSize: '13px', fontWeight: 600, color: t.c }}>
                {healthy
                  ? 'No goals failing this week'
                  : looping
                    // failedThisWeek.length, not a single row's .attempts — .attempts is
                    // that ROW's own retry counter, not how many times this fingerprint
                    // has been (re-)proposed, which is what "recurring" is describing here.
                    ? `1 recurring goal failing (seen ${failedThisWeek.length} times)`
                    : `${distinctFps} goal${distinctFps === 1 ? '' : 's'} failing this week`}
              </span>
            </div>
          )
        })()}

        {/* Propose form */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginBottom: '4px' }}>
          <div>
            <label style={labelStyle}>Title</label>
            <input
              type="text"
              value={proposeTitle}
              onChange={e => { setProposeTitle(e.target.value); setProposeErr('') }}
              placeholder="Short goal title"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>Description</label>
            <textarea
              value={proposeDesc}
              onChange={e => { setProposeDesc(e.target.value); setProposeErr('') }}
              placeholder="Describe the goal in detail"
              rows={3}
              style={{ ...inputStyle, resize: 'vertical' }}
            />
          </div>
          <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 120px' }}>
              <label style={labelStyle}>Risk</label>
              <select value={proposeRisk} onChange={e => setProposeRisk(e.target.value)} style={selectStyle}>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </div>
            <div style={{ flex: '1 1 120px' }}>
              <label style={labelStyle}>Category</label>
              <select value={proposeCategory} onChange={e => setProposeCategory(e.target.value)} style={selectStyle}>
                {categories.map(c => (
                  <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
                ))}
              </select>
            </div>
            <div style={{ flex: '1 1 120px' }}>
              <label style={labelStyle}>Cadence</label>
              <select value={proposeCadence} onChange={e => setProposeCadence(e.target.value)} style={selectStyle}>
                <option value="">One-shot</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </div>
          </div>
          <div>
            <label style={labelStyle}>
              Success criteria <span style={{ color: '#5d6982', textTransform: 'none', letterSpacing: 0 }}>(optional, for recurring goals)</span>
            </label>
            <input
              type="text"
              value={proposeSuccess}
              onChange={e => setProposeSuccess(e.target.value)}
              placeholder="A measurable check, e.g. 'Unraid usage < 85%'"
              style={inputStyle}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <PrimaryButton onClick={handlePropose} disabled={proposing}>
              {proposing ? 'Proposing...' : 'Propose goal'}
            </PrimaryButton>
            {proposeErr && (
              <span style={{ fontSize: '12px', color: '#fb7185' }}>{proposeErr}</span>
            )}
          </div>
        </div>

        {/* Divider */}
        <div style={{ height: '1px', background: 'rgba(120,160,220,0.10)', margin: '18px 0' }} />

        {/* Category filter */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' }}>
          <span style={labelStyle}>Filter by category:</span>
          <select
            value={categoryFilter}
            onChange={e => setCategoryFilter(e.target.value)}
            style={{ ...selectStyle, width: 'auto', padding: '7px 10px' }}
          >
            <option value="all">All</option>
            {categories.map(c => (
              <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
            ))}
          </select>
        </div>

        {/* Goals list */}
        {goals.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No goals yet.</span>
        ) : (
          <div>
            {goals.filter(g => categoryFilter === 'all' || g.category === categoryFilter).map((g) => (
              <div
                key={g.id}
                style={{
                  background: 'rgba(255,255,255,0.022)',
                  border: '1px solid rgba(120,160,220,0.08)',
                  borderRadius: '12px',
                  padding: '14px 16px',
                  marginBottom: '8px',
                }}
              >
                {/* Badges row */}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center' }}>
                  <Badge label={g.status} t={toneStatus(g.status)} />
                  <Badge label={g.risk || 'medium'} t={toneRisk(g.risk)} />
                  {g.category && (
                    <Badge label={g.category} t={{ c: '#2fd4ee', bg: 'rgba(47,212,238,0.08)', bd: 'rgba(47,212,238,0.30)' }} />
                  )}
                  {g.disabled && (
                    <Badge label="Disabled" t={{ c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }} />
                  )}
                  <span style={{ marginLeft: 'auto', fontSize: '11px', color: '#5d6982' }}>
                    {relativeTime(g.created_at)}
                  </span>
                </div>

                {/* Title */}
                <div style={{
                  fontSize: '14px', fontWeight: 600, color: '#dbe3f0',
                  marginTop: '6px',
                  textDecoration: g.disabled ? 'line-through' : 'none',
                  opacity: g.disabled ? 0.6 : 1,
                }}>
                  {g.title}
                </div>

                {/* Running: show task_id */}
                {(g.status === 'running' || g.status === 'approved') && g.task_id && (
                  <div style={{ fontSize: '12px', color: '#2fd4ee', marginTop: '4px', fontFamily: "'JetBrains Mono', monospace" }}>
                    task #{g.task_id}
                  </div>
                )}

                {/* Failed: reason */}
                {g.status === 'failed' && g.rejection_reason && (
                  <div style={{ fontSize: '12px', color: '#fb7185', marginTop: '4px' }}>
                    {g.rejection_reason}
                  </div>
                )}

                {/* Inline edit form */}
                {editingGoalId === g.id ? (
                  <div style={{
                    marginTop: '12px', padding: '14px',
                    borderRadius: '10px', border: '1px solid rgba(120,160,220,0.20)',
                    display: 'flex', flexDirection: 'column', gap: '10px',
                  }}>
                    <div>
                      <label style={labelStyle}>Title</label>
                      <input
                        type="text"
                        value={editFields.title}
                        onChange={e => setEditFields(f => ({ ...f, title: e.target.value }))}
                        style={inputStyle}
                      />
                    </div>
                    <div>
                      <label style={labelStyle}>Description</label>
                      <textarea
                        value={editFields.description}
                        onChange={e => setEditFields(f => ({ ...f, description: e.target.value }))}
                        rows={3}
                        style={{ ...inputStyle, resize: 'vertical' }}
                      />
                    </div>
                    <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                      <div style={{ flex: '1 1 120px' }}>
                        <label style={labelStyle}>Risk</label>
                        <select
                          value={editFields.risk}
                          onChange={e => setEditFields(f => ({ ...f, risk: e.target.value }))}
                          style={selectStyle}
                        >
                          <option value="low">Low</option>
                          <option value="medium">Medium</option>
                          <option value="high">High</option>
                        </select>
                      </div>
                      <div style={{ flex: '1 1 120px' }}>
                        <label style={labelStyle}>Category</label>
                        <select
                          value={editFields.category}
                          onChange={e => setEditFields(f => ({ ...f, category: e.target.value }))}
                          style={selectStyle}
                        >
                          {categories.map(c => (
                            <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                      <PrimaryButton onClick={() => handleSaveEdit(g.id)} disabled={goalActingId === g.id}>
                        {goalActingId === g.id ? 'Saving...' : 'Save'}
                      </PrimaryButton>
                      <button
                        onClick={handleCancelEdit}
                        style={{
                          border: '1px solid rgba(120,160,220,0.20)',
                          background: 'transparent',
                          color: '#8a96ad',
                          padding: '7px 14px',
                          borderRadius: '8px',
                          fontSize: '13px',
                          cursor: 'pointer',
                          fontFamily: 'inherit',
                        }}
                      >
                        Cancel
                      </button>
                      {goalErrors[g.id] && (
                        <span style={{ fontSize: '12px', color: '#fb7185' }}>{goalErrors[g.id]}</span>
                      )}
                    </div>
                  </div>
                ) : (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginTop: '10px' }}>
                    {g.status === 'proposed' && (
                      <>
                        <PrimaryButton onClick={() => handleGoalApprove(g.id)} disabled={goalActingId === g.id}>
                          {goalActingId === g.id ? 'Approving...' : 'Approve'}
                        </PrimaryButton>
                        <button
                          onClick={() => handleGoalReject(g.id)}
                          disabled={goalActingId === g.id}
                          style={{
                            border: '1px solid rgba(120,160,220,0.20)',
                            background: 'transparent',
                            color: '#8a96ad',
                            padding: '7px 14px',
                            borderRadius: '8px',
                            fontSize: '13px',
                            cursor: goalActingId === g.id ? 'not-allowed' : 'pointer',
                            opacity: goalActingId === g.id ? 0.5 : 1,
                            fontFamily: 'inherit',
                          }}
                        >
                          {goalActingId === g.id ? 'Rejecting...' : 'Reject'}
                        </button>
                      </>
                    )}
                    <button
                      onClick={() => handleStartEdit(g)}
                      disabled={goalActingId === g.id}
                      style={{
                        border: 'none',
                        background: 'transparent',
                        color: 'var(--accent)',
                        padding: '7px 12px',
                        borderRadius: '8px',
                        fontSize: '13px',
                        cursor: goalActingId === g.id ? 'not-allowed' : 'pointer',
                        opacity: goalActingId === g.id ? 0.5 : 1,
                        fontFamily: 'inherit',
                      }}
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => handleGoalToggleDisabled(g)}
                      disabled={goalActingId === g.id}
                      style={{
                        border: 'none',
                        background: 'transparent',
                        color: '#8a96ad',
                        padding: '7px 12px',
                        borderRadius: '8px',
                        fontSize: '13px',
                        cursor: goalActingId === g.id ? 'not-allowed' : 'pointer',
                        opacity: goalActingId === g.id ? 0.5 : 1,
                        fontFamily: 'inherit',
                      }}
                    >
                      {goalActingId === g.id ? '...' : (g.disabled ? 'Enable' : 'Disable')}
                    </button>
                    <button
                      onClick={() => handleGoalDelete(g.id)}
                      disabled={goalActingId === g.id}
                      style={{
                        border: 'none',
                        background: 'transparent',
                        color: '#fb7185',
                        padding: '7px 12px',
                        borderRadius: '8px',
                        fontSize: '13px',
                        cursor: goalActingId === g.id ? 'not-allowed' : 'pointer',
                        opacity: goalActingId === g.id ? 0.5 : 1,
                        fontFamily: 'inherit',
                      }}
                    >
                      {goalActingId === g.id ? '...' : 'Delete'}
                    </button>
                    {goalErrors[g.id] && (
                      <span style={{ fontSize: '12px', color: '#fb7185' }}>{goalErrors[g.id]}</span>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 6. Verifications                                                     */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Recent Verdicts</Eyebrow>
        {outcomes === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : outcomes.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No verdicts yet.</span>
        ) : (
          <div>
            {outcomes.map((o) => (
              <div
                key={o.id}
                style={{
                  display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: '10px',
                  padding: '12px 14px', borderRadius: '11px',
                  background: 'rgba(255,255,255,0.022)',
                  border: '1px solid rgba(120,160,220,0.08)',
                  marginBottom: '6px',
                }}
              >
                {/* Row 1 */}
                <Badge label={o.verdict || 'unknown'} t={tone(o.verdict)} />
                <span style={{ fontSize: '13px', color: '#8a96ad' }}>
                  {Math.round((o.confidence ?? 0) * 100)}%
                </span>
                {o.grounded && (
                  <Badge label="Grounded" t={{ c: '#2fd4ee', bg: 'rgba(47,212,238,0.08)', bd: 'rgba(47,212,238,0.32)' }} />
                )}
                <span style={{ fontSize: '12px', color: '#5d6982', fontFamily: "'JetBrains Mono', monospace" }}>
                  task #{o.task_id}
                </span>
                <span style={{ marginLeft: 'auto', fontSize: '11px', color: '#5d6982' }}>
                  {relativeTime(o.created_at)}
                </span>
                {/* Row 2 */}
                {o.reason && (
                  <div style={{ width: '100%', fontSize: '13px', color: '#aab4c7', lineHeight: 1.55, overflowWrap: 'anywhere' }}>
                    {o.reason}
                  </div>
                )}
                {o.evidence && (
                  <div style={{
                    width: '100%', fontSize: '12px', color: '#5d6982', lineHeight: 1.5,
                    fontFamily: "'JetBrains Mono', monospace", overflowWrap: 'anywhere',
                  }}>
                    {o.evidence}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 7. Metering Health                                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Metering Health</Eyebrow>
        {metering === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            {/* Prices verified status */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <StatusDot
                color={metering.prices_verified ? '#34d399' : '#fbbf24'}
                size={7}
                glow
              />
              <span style={{
                fontSize: '13px', fontWeight: 600,
                color: metering.prices_verified ? '#5fe0b4' : '#f4d27a',
              }}>
                {metering.prices_verified ? 'Prices verified' : 'Prices unverified — cost caps may be inaccurate'}
              </span>
            </div>

            {/* Today stats */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))', gap: '14px' }}>
              <div>
                <div style={labelStyle}>Today spend</div>
                <div style={{ fontSize: '15px', fontWeight: 700, color: 'var(--accent)' }}>
                  {fmtUsd(metering.today_spend_usd)}
                </div>
              </div>
              <div>
                <div style={labelStyle}>Rows today</div>
                <div style={{ fontSize: '15px', fontWeight: 700, color: '#e9eef8' }}>
                  {metering.today_row_count ?? 0}
                </div>
              </div>
            </div>

            {/* Counters */}
            {metering.counters && (
              <div>
                <Eyebrow style={{ display: 'block', marginBottom: '10px' }}>Spend log counters</Eyebrow>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))', gap: '14px' }}>
                  <div>
                    <div style={labelStyle}>Recorded</div>
                    <div style={{ fontSize: '15px', fontWeight: 700, color: '#5fe0b4' }}>
                      {metering.counters.recorded ?? 0}
                    </div>
                  </div>
                  <div>
                    <div style={labelStyle}>Skipped (no usage)</div>
                    <div style={{ fontSize: '15px', fontWeight: 700, color: '#8a96ad' }}>
                      {metering.counters.skipped_no_usage ?? 0}
                    </div>
                  </div>
                  <div>
                    <div style={labelStyle}>Skipped (unparseable)</div>
                    <div style={{ fontSize: '15px', fontWeight: 700, color: '#f4d27a' }}>
                      {metering.counters.skipped_unparseable ?? 0}
                    </div>
                  </div>
                  <div>
                    <div style={labelStyle}>Failed</div>
                    <div style={{ fontSize: '15px', fontWeight: 700, color: '#fb7185' }}>
                      {metering.counters.failed ?? 0}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 8. Today's Spend                                                     */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '10px' }}>Today's Spend</Eyebrow>
        {status === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : (
          <SpendBar spend={status.today_spend_usd ?? 0} budget={status.daily_budget_usd ?? 25} />
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 9. Budget Caps Editor                                                */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Budget Caps</Eyebrow>
        {status === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
              <div>
                <label style={labelStyle}>Daily limit (USD)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  value={dailyInput}
                  onChange={e => { setDailyInput(e.target.value); setBudgetSaved(false); setBudgetErr('') }}
                  style={inputStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>Per-task limit (USD)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  value={perTaskInput}
                  onChange={e => { setPerTaskInput(e.target.value); setBudgetSaved(false); setBudgetErr('') }}
                  style={inputStyle}
                />
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
              <button
                onClick={handleSaveBudget}
                style={{
                  border: '1px solid rgba(251,191,36,0.4)',
                  background: 'rgba(251,191,36,0.08)',
                  color: '#fbbf24',
                  padding: '9px 16px',
                  borderRadius: '10px',
                  fontWeight: 700,
                  fontSize: '13px',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                Save caps
              </button>
              {budgetSaved && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <StatusDot color="#34d399" size={7} glow />
                  <span style={{ fontSize: '13px', color: '#5fe0b4' }}>Saved</span>
                </div>
              )}
              {budgetErr && (
                <span style={{ fontSize: '13px', color: '#fb7185' }}>{budgetErr}</span>
              )}
            </div>
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 10. Recent Actions                                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Recent Actions</Eyebrow>
        {actions === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : actions.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No actions logged yet.</span>
        ) : (
          <div>
            {actions.map((a) => (
              // Badges + actor/kind + time on the top line (wrapping as needed);
              // target, judge reason, and failure error each get a full-width line
              // that WRAPS — never ellipsis-truncated. Shadow-mode judge review
              // (Brian reading real verdicts before flipping enforce) depends on
              // the FULL reason being readable, including on a ~375px phone.
              <div key={a.id} style={{ ...rowStyle, alignItems: 'flex-start' }}>
                <Badge label={a.decision || 'unknown'} t={tone(a.decision)} />
                {a.judge_verdict != null && (
                  <Badge label={`judge: ${a.judge_verdict}`} t={tone(a.judge_verdict)} />
                )}
                <span style={{ fontSize: '12px', color: '#8a96ad' }}>
                  {[a.actor, a.kind].filter(Boolean).join(' / ')}
                </span>
                <span style={{ marginLeft: 'auto', fontSize: '11px', color: '#5d6982', flex: 'none' }}>
                  {relativeTime(a.created_at)}
                </span>
                {a.target && (
                  <span style={{
                    width: '100%', fontSize: '13px', color: '#dbe3f0',
                    fontFamily: "'JetBrains Mono', monospace",
                    overflowWrap: 'anywhere', lineHeight: 1.45,
                  }}>
                    {a.target}
                  </span>
                )}
                {a.judge_reason && (
                  <div style={{ width: '100%', fontSize: '13px', color: '#aab4c7', lineHeight: 1.55, overflowWrap: 'anywhere' }}>
                    Judge: {a.judge_reason}
                  </div>
                )}
                {a.decision === 'failed' && a.result && typeof a.result === 'object' && a.result.error && (
                  <div style={{ width: '100%', fontSize: '12px', color: '#fb7185', lineHeight: 1.5, overflowWrap: 'anywhere' }}>
                    {String(a.result.error)}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

    </div>
  )
}
