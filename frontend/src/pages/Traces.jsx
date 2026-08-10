import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import ScreenHeader from '../components/ScreenHeader'
import TextInput from '../components/TextInput'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// NEXUS stamps trace/span timestamps as naive UTC (datetime.utcnow().isoformat(),
// no trailing 'Z') — parse with the same '+Z' convention used elsewhere
// (Safety.jsx daysSince/ActionLog), or a bare new Date(iso) reads as local
// time and skews relative age / duration by the UTC offset.
function toMs(iso) {
  if (!iso) return null
  const t = new Date(iso.endsWith('Z') ? iso : iso + 'Z').getTime()
  return Number.isNaN(t) ? null : t
}

function relativeTime(iso) {
  const t = toMs(iso)
  if (t === null) return ''
  const diff = Math.floor((Date.now() - t) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`
}

function traceDurationMs(startedAt, endedAt) {
  const s = toMs(startedAt)
  const e = toMs(endedAt)
  if (s === null || e === null) return null
  return e - s
}

function fmtUsd(n) {
  if (n === null || n === undefined) return null
  const v = Number(n)
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

const toneStatus = (s) => {
  if (!s) return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
  const u = s.toLowerCase()
  if (u === 'ok' || u === 'completed' || u === 'success')
    return { c: '#5fe0b4', bg: 'rgba(52,211,153,0.08)', bd: 'rgba(52,211,153,0.25)' }
  if (u === 'running' || u === 'started')
    return { c: '#2fd4ee', bg: 'rgba(47,212,238,0.08)', bd: 'rgba(47,212,238,0.30)' }
  if (u === 'error' || u === 'failed')
    return { c: '#fb7185', bg: 'rgba(251,113,133,0.08)', bd: 'rgba(251,113,133,0.30)' }
  return { c: '#8a96ad', bg: 'rgba(120,160,220,0.08)', bd: 'rgba(120,160,220,0.14)' }
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

const selectStyle = {
  background: 'rgba(255,255,255,0.03)',
  color: '#e9eef8',
  border: '1px solid rgba(120,160,220,0.16)',
  borderRadius: '10px',
  padding: '7px 10px',
  fontSize: '12px',
  outline: 'none',
  cursor: 'pointer',
  appearance: 'none',
  WebkitAppearance: 'none',
  fontFamily: "'Space Grotesk', sans-serif",
}

const rowStyle = {
  display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: '6px',
  padding: '11px 14px', borderRadius: '11px',
  background: 'rgba(255,255,255,0.022)',
  border: '1px solid rgba(120,160,220,0.08)',
  marginBottom: '6px',
  cursor: 'pointer',
}

const spanRowStyle = {
  display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap',
  padding: '9px 12px', borderRadius: '9px',
  background: 'rgba(255,255,255,0.018)',
  border: '1px solid rgba(120,160,220,0.06)',
  marginBottom: '5px',
}

const KINDS = ['all', 'chat', 'briefing', 'orchestrator', 'proposer', 'voice']

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Traces() {
  const [traces, setTraces] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [kindFilter, setKindFilter] = useState('all')
  const [q, setQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
  const [expandedId, setExpandedId] = useState(null)
  const [spansById, setSpansById] = useState({})
  const [spansLoadingId, setSpansLoadingId] = useState(null)
  const [spansErrors, setSpansErrors] = useState({})
  const [expandedSpanId, setExpandedSpanId] = useState(null)

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300)
    return () => clearTimeout(t)
  }, [q])

  const load = useCallback(() => {
    api.traces.list(50, kindFilter === 'all' ? null : kindFilter, debouncedQ || null)
      .then(t => { setTraces(t); setLoadError(null) })
      .catch(err => setLoadError(err?.message || 'Failed to load traces.'))
  }, [kindFilter, debouncedQ])

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    return () => clearInterval(timer)
  }, [load])

  async function toggleExpand(id) {
    if (expandedId === id) {
      setExpandedId(null)
      return
    }
    setExpandedId(id)
    if (spansById[id]) return
    setSpansLoadingId(id)
    try {
      const detail = await api.traces.get(id)
      setSpansById(prev => ({ ...prev, [id]: detail.spans }))
    } catch (err) {
      setSpansErrors(prev => ({ ...prev, [id]: err?.message || 'Failed to load spans.' }))
    } finally {
      setSpansLoadingId(null)
    }
  }

  return (
    <div style={{
      width: '100%', maxWidth: '1100px', margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex', flexDirection: 'column', gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Traces" title="Agent Traces" />

      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '10px' }}>
          <Eyebrow>Recent Traces</Eyebrow>
          <TextInput
            placeholder="Search traces…"
            value={q}
            onChange={e => setQ(e.target.value)}
            style={{ flex: '1 1 180px', minWidth: 0, padding: '7px 10px', fontSize: '13px' }}
          />
          <select
            value={kindFilter}
            onChange={e => setKindFilter(e.target.value)}
            style={selectStyle}
          >
            {KINDS.map(k => (
              <option key={k} value={k}>{k === 'all' ? 'All kinds' : k}</option>
            ))}
          </select>
        </div>

        {loadError ? (
          <span style={{ fontSize: '13px', color: '#fb7185' }}>{loadError}</span>
        ) : traces === null ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>Loading...</span>
        ) : traces.length === 0 ? (
          <span style={{ fontSize: '13px', color: '#5d6982' }}>
            {debouncedQ ? `No traces match "${debouncedQ}".` : 'No traces yet.'}
          </span>
        ) : (
          <div>
            {traces.map(t => (
              <div key={t.id}>
                <div style={rowStyle} onClick={() => toggleExpand(t.id)}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                    <Badge label={t.status || 'unknown'} t={toneStatus(t.status)} />
                    <span style={{ fontSize: '12px', color: '#8a96ad' }}>{t.kind}</span>
                    <span style={{ flex: 1 }} />
                    {t.span_count != null && (
                      <span style={{ fontSize: '11px', color: '#5d6982' }}>
                        {t.span_count === 0 ? 'no spans' : `${t.span_count} span${t.span_count === 1 ? '' : 's'}`}
                      </span>
                    )}
                    {fmtUsd(t.total_cost_usd) && (
                      <span style={{ fontSize: '11px', color: 'var(--accent)' }}>
                        {fmtUsd(t.total_cost_usd)}
                      </span>
                    )}
                    <span style={{ fontSize: '12px', color: '#5d6982', fontFamily: "'JetBrains Mono', monospace", flex: 'none' }}>
                      {fmtMs(traceDurationMs(t.started_at, t.ended_at))}
                    </span>
                    <span style={{ fontSize: '11px', color: '#5d6982', flex: 'none' }}>
                      {relativeTime(t.started_at)}
                    </span>
                  </div>
                  <span style={{
                    fontSize: '13px', color: '#dbe3f0',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {t.label || `trace #${t.id}`}
                  </span>
                </div>

                {t.status && t.status.toLowerCase() === 'error' && t.error && (
                  <div style={{ fontSize: '12px', color: '#fb7185', margin: '-2px 0 6px 14px', overflowWrap: 'anywhere' }}>
                    {t.error}
                  </div>
                )}

                {expandedId === t.id && (
                  <div style={{ margin: '0 0 10px 14px' }}>
                    {spansLoadingId === t.id ? (
                      <span style={{ fontSize: '12px', color: '#5d6982' }}>Loading spans...</span>
                    ) : spansErrors[t.id] ? (
                      <span style={{ fontSize: '12px', color: '#fb7185' }}>{spansErrors[t.id]}</span>
                    ) : (spansById[t.id] || []).length === 0 ? (
                      <span style={{ fontSize: '12px', color: '#5d6982' }}>No spans recorded.</span>
                    ) : (
                      <>
                        {(() => {
                          const spans = spansById[t.id]
                          const tin = spans.reduce((a, s) => (s.tokens_in != null ? (a ?? 0) + s.tokens_in : a), null)
                          const tout = spans.reduce((a, s) => (s.tokens_out != null ? (a ?? 0) + s.tokens_out : a), null)
                          const cost = spans.reduce((a, s) => (s.cost_usd != null ? (a ?? 0) + s.cost_usd : a), null)
                          const tokenStr = (tin != null || tout != null) ? `${tin ?? 0}in / ${tout ?? 0}out` : null
                          const costStr = fmtUsd(cost)
                          return (
                            <div style={{
                              fontSize: '11px', color: '#8a96ad', fontFamily: "'JetBrains Mono', monospace",
                              marginBottom: '6px', display: 'flex', gap: '10px', flexWrap: 'wrap',
                            }}>
                              <span>{spans.length} span{spans.length === 1 ? '' : 's'}</span>
                              {tokenStr && <span>{tokenStr}</span>}
                              {costStr && <span>{costStr}</span>}
                            </div>
                          )
                        })()}
                        {spansById[t.id].map(s => {
                        const hasDetail = !!(s.input_summary || s.output_summary)
                        const isOpen = expandedSpanId === s.id
                        return (
                          <div
                            key={s.id}
                            style={{ ...spanRowStyle, cursor: hasDetail ? 'pointer' : 'default' }}
                            onClick={() => { if (hasDetail) setExpandedSpanId(isOpen ? null : s.id) }}
                          >
                            <span style={{ fontSize: '11px', color: '#8a96ad', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                              {s.span_type}
                            </span>
                            <span style={{ fontSize: '12px', color: '#dbe3f0', fontFamily: "'JetBrains Mono', monospace" }}>
                              {s.name}
                            </span>
                            <span style={{ fontSize: '11px', color: '#5d6982' }}>
                              {fmtMs(s.duration_ms)}
                            </span>
                            {(s.tokens_in != null || s.tokens_out != null) && (
                              <span style={{ fontSize: '11px', color: '#5d6982' }}>
                                {s.tokens_in ?? 0}in / {s.tokens_out ?? 0}out
                              </span>
                            )}
                            {fmtUsd(s.cost_usd) && (
                              <span style={{ fontSize: '11px', color: 'var(--accent)' }}>
                                {fmtUsd(s.cost_usd)}
                              </span>
                            )}
                            {hasDetail && (
                              <span style={{ fontSize: '11px', color: '#5d6982', flex: 'none' }}>
                                {isOpen ? '⌄' : '›'}
                              </span>
                            )}
                            {s.error && (
                              <span style={{ fontSize: '11px', color: '#fb7185', flexBasis: '100%', overflowWrap: 'anywhere' }}>
                                {s.error}
                              </span>
                            )}
                            {hasDetail && !isOpen && (
                              <span style={{
                                flexBasis: '100%', fontSize: '11px', color: '#5d6982',
                                fontFamily: "'JetBrains Mono', monospace",
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                              }}>
                                {(s.input_summary || s.output_summary).replace(/\s+/g, ' ').slice(0, 160)}
                              </span>
                            )}
                            {isOpen && (
                              <div style={{ flexBasis: '100%', display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '4px' }}>
                                {s.input_summary && (
                                  <div>
                                    <div style={{ fontSize: '10px', color: '#8a96ad', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '3px' }}>
                                      Input
                                    </div>
                                    <div style={{
                                      fontSize: '11px', color: '#c3ccdb', fontFamily: "'JetBrains Mono', monospace",
                                      whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
                                      background: 'rgba(0,0,0,0.15)', border: '1px solid rgba(120,160,220,0.08)',
                                      borderRadius: '7px', padding: '8px 10px',
                                    }}>
                                      {s.input_summary}
                                    </div>
                                  </div>
                                )}
                                {s.output_summary && (
                                  <div>
                                    <div style={{ fontSize: '10px', color: '#8a96ad', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '3px' }}>
                                      Output
                                    </div>
                                    <div style={{
                                      fontSize: '11px', color: '#c3ccdb', fontFamily: "'JetBrains Mono', monospace",
                                      whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
                                      background: 'rgba(0,0,0,0.15)', border: '1px solid rgba(120,160,220,0.08)',
                                      borderRadius: '7px', padding: '8px 10px',
                                    }}>
                                      {s.output_summary}
                                    </div>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        )
                      })}
                      </>
                    )}
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
