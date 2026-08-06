import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'
import GhostButton from '../components/GhostButton'
import { parseUTC, fmtDateTime } from '../lib/parseUTC'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Backend stamps naive UTC (no trailing 'Z') — must go through parseUTC, not
// a bare `new Date(iso)`, or every relative time skews by the local UTC
// offset. (Safety.jsx's own local relativeTime helper has exactly this bug —
// deliberately not reused here.)
function relativeTime(isoStr) {
  if (!isoStr) return ''
  const t = parseUTC(isoStr).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Math.floor((Date.now() - t) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

// Same three hex values Safety.jsx's toneRisk uses for low/medium/high.
const toneSeverity = (s) => {
  const u = (s || '').toLowerCase()
  if (u === 'low') return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (u === 'high') return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
  return { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' } // medium (default)
}

// Deliberately NOT Safety.jsx's toneStatus (that's keyed to Goal statuses —
// "completed"/"proposed"/"running"/"failed" — and would misrender every
// OutcomeFlag status). Vocabulary: open|resolved|deferred|false_positive|
// needs_follow_up (backend/agents/outcomes.py).
const toneFlagStatus = (s) => {
  const u = (s || '').toLowerCase()
  if (u === 'open') return { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }
  if (u === 'needs_follow_up') return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
  if (u === 'deferred') return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  if (u === 'resolved') return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (u === 'false_positive') return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
}

// The API's ?status= is exact-match on one literal value only — "open" as a
// concept the user cares about is a compound the backend derives server-side
// (_db_open_flags) but never expresses back over the wire. Mirrored here so
// the default "active" filter and the health tiles agree with the backend's
// own definition of "still needs attention".
function isActiveFlag(f) {
  if (f.status === 'open' || f.status === 'needs_follow_up') return true
  if (f.status === 'deferred' && f.deferred_until) {
    const t = parseUTC(f.deferred_until).getTime()
    return !Number.isNaN(t) && t <= Date.now()
  }
  return false
}

function isTerminalFlag(f) {
  return f.status === 'resolved' || f.status === 'false_positive'
}

const SEVERITY_ORDER = ['high', 'medium', 'low']

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
// Inline styles for form fields (copied from Safety.jsx — no shared style
// module exists in this repo, duplicating locally is the established
// convention there).
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

// flexWrap is load-bearing (see Safety.jsx's identical comment): without it a
// narrow viewport squeezes the summary/detail lines to zero width instead of
// wrapping onto their own full-width row.
const rowStyle = {
  display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: '12px',
  padding: '11px 14px', borderRadius: '11px',
  background: 'rgba(255,255,255,0.022)',
  border: '1px solid rgba(120,160,220,0.08)',
  marginBottom: '6px',
}

// Destructive-secondary button — copied verbatim from Safety.jsx's
// "Clear N stuck" dead-letter button, the one real destructive-but-secondary
// (red border+bg, not a filled Primary) button that file defines.
const destructiveButtonStyle = (disabled) => ({
  border: '1px solid rgba(251,113,133,0.35)',
  background: 'rgba(251,113,133,0.08)',
  color: '#fb7185',
  padding: '7px 14px',
  borderRadius: '8px',
  fontWeight: 700,
  fontSize: '12px',
  cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.5 : 1,
  fontFamily: 'inherit',
})

// ---------------------------------------------------------------------------
// Row renderer — shared by the active list (Card 2) and recently-closed
// (Card 3); `showActions` hides the resolve/defer controls for terminal rows.
// ---------------------------------------------------------------------------

function FlagRow({
  f, showActions, acting, error, note, onNoteChange, deferDays, onDeferDaysChange,
  onResolve, onFalseAlarm, onDefer,
}) {
  return (
    <div style={{ ...rowStyle, alignItems: 'flex-start' }}>
      <Badge label={f.severity || 'medium'} t={toneSeverity(f.severity)} />
      <Badge label={f.status} t={toneFlagStatus(f.status)} />
      {f.suppressed && (
        <Badge label="Suppressed" t={{ c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }} />
      )}
      <span style={{ fontSize: '12px', color: '#8a96ad', fontFamily: "'JetBrains Mono', monospace" }}>
        {f.source}:{f.check}
      </span>
      {f.surfaced_count > 1 && (
        <span style={{ fontSize: '11px', color: '#5d6982' }}>seen {f.surfaced_count}x</span>
      )}
      <span style={{ marginLeft: 'auto', fontSize: '11px', color: '#5d6982', flex: 'none' }}>
        {relativeTime(f.created_at)}
      </span>

      {/* Full-width lines — never ellipsis-truncated */}
      <span style={{
        width: '100%', fontSize: '13px', color: '#dbe3f0',
        overflowWrap: 'anywhere', lineHeight: 1.45,
      }}>
        {f.summary}
      </span>
      {f.detail && (
        <div style={{
          width: '100%', fontSize: '12px', color: '#8a96ad', lineHeight: 1.5,
          fontFamily: "'JetBrains Mono', monospace", overflowWrap: 'anywhere',
        }}>
          {f.detail}
        </div>
      )}
      {f.status === 'deferred' && f.deferred_until && (
        <div style={{ width: '100%', fontSize: '12px', color: '#8a96ad' }}>
          Deferred until {fmtDateTime(f.deferred_until)}
        </div>
      )}
      {!showActions && f.resolved_at && (
        <div style={{ width: '100%', fontSize: '12px', color: '#5d6982' }}>
          Closed {relativeTime(f.resolved_at)}{f.resolved_by ? ` by ${f.resolved_by}` : ''}
        </div>
      )}
      {!showActions && f.resolution_note && (
        <div style={{ width: '100%', fontSize: '12px', color: '#8a96ad', overflowWrap: 'anywhere' }}>
          Note: {f.resolution_note}
        </div>
      )}

      {showActions && (
        <div style={{ width: '100%', display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <input
            type="text"
            value={note || ''}
            onChange={e => onNoteChange(e.target.value)}
            placeholder="Optional note..."
            style={{ ...inputStyle, fontSize: '12px', padding: '7px 10px' }}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
            <PrimaryButton onClick={onResolve} disabled={acting}>
              {acting ? 'Working...' : 'Resolve'}
            </PrimaryButton>
            <button onClick={onFalseAlarm} disabled={acting} style={destructiveButtonStyle(acting)}>
              False alarm
            </button>
            <input
              type="number"
              min={1}
              max={365}
              value={deferDays === undefined ? 7 : deferDays}
              onChange={e => onDeferDaysChange(e.target.value)}
              style={{ ...inputStyle, width: '70px', padding: '7px 8px', fontSize: '12px' }}
            />
            <GhostButton onClick={onDefer} disabled={acting}>
              Defer (days)
            </GhostButton>
            {error && <span style={{ fontSize: '12px', color: '#fb7185' }}>{error}</span>}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Flags() {
  const [flags, setFlags] = useState(null)
  const [calib, setCalib] = useState(null)

  const [statusFilter, setStatusFilter] = useState('active')
  const [sourceFilter, setSourceFilter] = useState('all')
  const [showAllClosed, setShowAllClosed] = useState(false)

  const [actingId, setActingId] = useState(null)
  const [rowErrors, setRowErrors] = useState({})
  const [noteById, setNoteById] = useState({})
  const [deferDaysById, setDeferDaysById] = useState({})

  // Create-flag form state
  const [checkInput, setCheckInput] = useState('')
  const [summaryInput, setSummaryInput] = useState('')
  const [detailInput, setDetailInput] = useState('')
  const [severityInput, setSeverityInput] = useState('medium')
  const [creating, setCreating] = useState(false)
  const [createStatus, setCreateStatus] = useState(null) // { kind: 'error'|'notice'|'success', text }

  // ---------------------------------------------------------------------------
  // REST load (30s poll — flag writes only ever happen on the 5-min watchdog
  // tick, the 60s homelab loop, or the daily briefing; nothing changes faster).
  // ---------------------------------------------------------------------------
  const load = useCallback(() => {
    api.safety.flags(200).then(setFlags).catch(() => {})
    api.safety.flagsCalibration(30).then(setCalib).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 30000)
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
  // Action handler — mirrors Safety.jsx's handleConfirm pattern.
  // ---------------------------------------------------------------------------
  async function act(id, status, deferDays) {
    setActingId(id)
    setRowErrors(prev => ({ ...prev, [id]: '' }))
    try {
      await api.safety.resolveFlag(id, status, noteById[id], deferDays)
      setNoteById(prev => ({ ...prev, [id]: '' }))
      load()
    } catch (err) {
      const raw = err?.message || ''
      const msg = raw.startsWith('409') ? 'Already closed — this row now shows its true state.'
                : raw.startsWith('404') ? 'Flag not found — it may have been deleted.'
                : raw.startsWith('400') ? 'Invalid request — check the status or defer days.'
                : raw || 'Failed to update flag.'
      setRowErrors(prev => ({ ...prev, [id]: msg }))
      load()
    } finally {
      setActingId(null)
    }
  }

  function handleDefer(f) {
    const raw = deferDaysById[f.id]
    const days = Math.min(365, Math.max(1, Math.floor(Number(raw)) || 7))
    act(f.id, 'deferred', days)
  }

  async function handleCreateFlag() {
    if (!checkInput.trim()) { setCreateStatus({ kind: 'error', text: 'Check is required.' }); return }
    if (!summaryInput.trim()) { setCreateStatus({ kind: 'error', text: 'Summary is required.' }); return }
    setCreating(true)
    setCreateStatus(null)
    try {
      const res = await api.safety.createFlag(
        checkInput.trim(), summaryInput.trim(), detailInput.trim() || null, severityInput,
      )
      if (res && res.id === null) {
        // Non-error, non-success: deduped / cooldown / disabled. Never clear
        // the form here — Brian would otherwise lose whatever they typed.
        setCreateStatus({ kind: 'notice', text: 'Not recorded — deduped, suppressed, or outcome tracking is disabled.' })
      } else {
        setCreateStatus({ kind: 'success', text: 'Logged.' })
        setCheckInput('')
        setSummaryInput('')
        setDetailInput('')
        setSeverityInput('medium')
      }
      load()
    } catch (err) {
      setCreateStatus({ kind: 'error', text: err?.message || 'Failed to log flag.' })
    } finally {
      setCreating(false)
    }
  }

  // ---------------------------------------------------------------------------
  // Derived data
  // ---------------------------------------------------------------------------
  const sources = flags ? [...new Set(flags.map(f => f.source))].sort() : []
  const activeRows = flags ? flags.filter(isActiveFlag) : []

  const openCount = flags ? flags.filter(f => f.status === 'open').length : 0
  const needsFollowUpCount = flags ? flags.filter(f => f.status === 'needs_follow_up').length : 0
  const deferredCount = flags ? flags.filter(f => f.status === 'deferred').length : 0
  const recentlyClosed = flags
    ? flags
        .filter(f => isTerminalFlag(f) && f.resolved_at && (Date.now() - parseUTC(f.resolved_at).getTime()) < 7 * 86400000)
        .sort((a, b) => parseUTC(b.resolved_at).getTime() - parseUTC(a.resolved_at).getTime())
    : []

  const highActive = activeRows.filter(f => f.severity === 'high')

  let filteredRows = flags || []
  if (sourceFilter !== 'all') filteredRows = filteredRows.filter(f => f.source === sourceFilter)
  if (statusFilter === 'active') filteredRows = filteredRows.filter(isActiveFlag)
  else if (statusFilter !== 'all') filteredRows = filteredRows.filter(f => f.status === statusFilter)

  const severityGroups = SEVERITY_ORDER
    .map(sev => [sev, filteredRows.filter(f => (f.severity || 'medium') === sev)])
    .filter(([, rows]) => rows.length > 0)

  // Calibration rows: denominator is resolved+false_positive ONLY, matching
  // CalibrationHint.verdict_count's definition — open/deferred/needs_follow_up
  // are excluded from the rate, never from the row itself.
  const calibRows = calib
    ? Object.entries(calib).map(([key, counts]) => {
        const resolved = counts.resolved || 0
        const falsePositive = counts.false_positive || 0
        const openLike = (counts.open || 0) + (counts.needs_follow_up || 0) + (counts.deferred || 0)
        const denom = resolved + falsePositive
        const fpRate = denom > 0 ? falsePositive / denom : null
        return { key, resolved, falsePositive, openLike, denom, fpRate }
      })
    : []
  const sortedCalibRows = [...calibRows].sort((a, b) => {
    if (a.fpRate === null && b.fpRate !== null) return 1
    if (b.fpRate === null && a.fpRate !== null) return -1
    if (a.fpRate !== null && b.fpRate !== null && b.fpRate !== a.fpRate) return b.fpRate - a.fpRate
    const totalA = a.resolved + a.falsePositive + a.openLike
    const totalB = b.resolved + b.falsePositive + b.openLike
    return totalB - totalA
  })

  const toneFpRate = (rate) => {
    if (rate === null) return { c: '#5d6982', bg: 'rgba(120,160,220,0.06)', bd: 'rgba(120,160,220,0.12)' }
    if (rate < 0.3) return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
    if (rate < 0.6) return { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }
    return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
  }

  return (
    <div style={{
      width: '100%', maxWidth: '1100px', margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex', flexDirection: 'column', gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Flags" title="Outcome Flags" />

      {/* ------------------------------------------------------------------ */}
      {/* 1. Health + counts                                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card accent="cyan">
        {flags === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : (() => {
          const t = highActive.length > 0
            ? { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
            : activeRows.length > 0
              ? { c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }
              : { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
          const headline = highActive.length > 0
            ? `${highActive.length} high-severity flag${highActive.length === 1 ? '' : 's'} need attention`
            : activeRows.length > 0
              ? `${activeRows.length} open item${activeRows.length === 1 ? '' : 's'}`
              : 'No open flags'
          return (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '14px' }}>
                <StatusDot color={t.c} size={7} glow />
                <span style={{ fontSize: '14px', fontWeight: 600, color: t.c }}>{headline}</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(90px, 1fr))', gap: '14px' }}>
                <div>
                  <div style={labelStyle}>Open</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: '#e9eef8' }}>{openCount}</div>
                </div>
                <div>
                  <div style={labelStyle}>Needs follow-up</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: needsFollowUpCount > 0 ? '#fb7185' : '#e9eef8' }}>
                    {needsFollowUpCount}
                  </div>
                </div>
                <div>
                  <div style={labelStyle}>Deferred</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: '#e9eef8' }}>{deferredCount}</div>
                </div>
                <div>
                  <div style={labelStyle}>Closed (7d)</div>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: '#e9eef8' }}>{recentlyClosed.length}</div>
                </div>
              </div>
            </>
          )
        })()}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 2. Filters + active flag list                                        */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '14px', flexWrap: 'wrap' }}>
          <Eyebrow>Flags</Eyebrow>
          <span style={{ flex: 1 }} />
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={{ ...selectStyle, width: 'auto', padding: '7px 10px' }}>
              <option value="active">Active</option>
              <option value="all">All</option>
              <option value="open">Open</option>
              <option value="needs_follow_up">Needs follow-up</option>
              <option value="deferred">Deferred</option>
              <option value="resolved">Resolved</option>
              <option value="false_positive">False positive</option>
            </select>
            <select value={sourceFilter} onChange={e => setSourceFilter(e.target.value)} style={{ ...selectStyle, width: 'auto', padding: '7px 10px' }}>
              <option value="all">All sources</option>
              {sources.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>

        {flags === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : filteredRows.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No flags match this filter.</span>
        ) : (
          <div>
            {severityGroups.map(([sev, rows]) => (
              <div key={sev} style={{ marginBottom: '16px' }}>
                <Eyebrow style={{ display: 'block', marginBottom: '8px' }}>{sev}</Eyebrow>
                {rows.map(f => (
                  <FlagRow
                    key={f.id}
                    f={f}
                    showActions={!isTerminalFlag(f)}
                    acting={actingId === f.id}
                    error={rowErrors[f.id]}
                    note={noteById[f.id]}
                    onNoteChange={v => setNoteById(prev => ({ ...prev, [f.id]: v }))}
                    deferDays={deferDaysById[f.id]}
                    onDeferDaysChange={v => setDeferDaysById(prev => ({ ...prev, [f.id]: v }))}
                    onResolve={() => act(f.id, 'resolved')}
                    onFalseAlarm={() => act(f.id, 'false_positive')}
                    onDefer={() => handleDefer(f)}
                  />
                ))}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 3. Recently closed                                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: showAllClosed ? '14px' : '0' }}>
          <Eyebrow>Recently closed</Eyebrow>
          <span style={{ fontSize: '12px', color: '#5d6982' }}>({recentlyClosed.length} in the last 7 days)</span>
        </div>
        <button
          onClick={() => setShowAllClosed(v => !v)}
          style={{
            fontSize: '11px', fontWeight: 600, color: '#5d6982', background: 'none',
            border: 'none', cursor: 'pointer', padding: 0, marginTop: '4px',
          }}
        >
          {showAllClosed ? 'Hide' : `Show all ${recentlyClosed.length}`}
        </button>
        {showAllClosed && (
          <div style={{ marginTop: '10px' }}>
            {recentlyClosed.length === 0 ? (
              <span style={{ fontSize: '13px', color: '#5d6982' }}>Nothing closed in the last 7 days.</span>
            ) : (
              recentlyClosed.map(f => (
                <FlagRow key={f.id} f={f} showActions={false} />
              ))
            )}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 4. Calibration                                                       */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '6px' }}>Calibration</Eyebrow>
        <div style={{ fontSize: '11px', color: '#5d6982', marginBottom: '14px' }}>
          False-alarm rate over the last 30 days. Denominator counts resolved + false alarm only.
        </div>
        {calib === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : sortedCalibRows.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>No flags recorded in the last 30 days.</span>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {sortedCalibRows.map(row => {
              const t = toneFpRate(row.fpRate)
              return (
                <div key={row.key} style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', fontSize: '12px' }}>
                  <span style={{ color: '#dbe3f0', fontFamily: "'JetBrains Mono', monospace" }}>{row.key}</span>
                  <Badge label={`resolved ${row.resolved}`} t={{ c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }} />
                  <Badge label={`false_positive ${row.falsePositive}`} t={{ c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }} />
                  <Badge label={`open ${row.openLike}`} t={{ c: '#f4d27a', bg: 'rgba(251,191,36,0.08)', bd: 'rgba(251,191,36,0.30)' }} />
                  <span style={{ marginLeft: 'auto', color: t.c, fontWeight: 700 }}>
                    {row.fpRate === null ? '—' : `FP ${(row.fpRate * 100).toFixed(0)}% (${row.falsePositive}/${row.denom})`}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* 5. Log a flag                                                        */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '14px' }}>Log a flag</Eyebrow>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div>
            <label style={labelStyle}>Check</label>
            <input
              type="text"
              value={checkInput}
              onChange={e => { setCheckInput(e.target.value); setCreateStatus(null) }}
              placeholder="Short identifier, e.g. 'garage_open_check'"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>Summary</label>
            <input
              type="text"
              value={summaryInput}
              onChange={e => { setSummaryInput(e.target.value); setCreateStatus(null) }}
              placeholder="One-line description of what happened"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>
              Detail <span style={{ color: '#5d6982', textTransform: 'none', letterSpacing: 0 }}>(optional)</span>
            </label>
            <textarea
              value={detailInput}
              onChange={e => { setDetailInput(e.target.value); setCreateStatus(null) }}
              rows={3}
              style={{ ...inputStyle, resize: 'vertical' }}
            />
          </div>
          <div style={{ flex: '1 1 120px' }}>
            <label style={labelStyle}>Severity</label>
            <select value={severityInput} onChange={e => setSeverityInput(e.target.value)} style={{ ...selectStyle, width: '160px' }}>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <PrimaryButton onClick={handleCreateFlag} disabled={creating}>
              {creating ? 'Logging...' : 'Log flag'}
            </PrimaryButton>
            {createStatus && (
              <span style={{
                fontSize: '12px',
                color: createStatus.kind === 'error' ? '#fb7185' : createStatus.kind === 'notice' ? '#f4d27a' : '#5fe0b4',
              }}>
                {createStatus.text}
              </span>
            )}
          </div>
        </div>
      </Card>
    </div>
  )
}
