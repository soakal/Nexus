import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'
import TextInput from '../components/TextInput'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(isoStr) {
  if (!isoStr) return ''
  const diff = Math.floor((Date.now() - new Date(isoStr + 'Z').getTime()) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Facts() {
  const [facts, setFacts]             = useState(null)
  const [recallQuery, setRecallQuery] = useState('')
  const [recallResult, setRecallResult] = useState(null)
  const [recalling, setRecalling]     = useState(false)
  const [dismissingId, setDismissingId] = useState(null)

  // ---------------------------------------------------------------------------
  // REST load + 10s poll
  // ---------------------------------------------------------------------------
  const load = useCallback(() => {
    api.facts.list().then(setFacts).catch(() => {})
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
  // Handlers
  // ---------------------------------------------------------------------------
  async function handleRecall() {
    if (!recallQuery.trim() || recalling) return
    setRecalling(true)
    setRecallResult(null)
    try {
      const data = await api.facts.recall(recallQuery.trim())
      setRecallResult(data)
    } catch {
      setRecallResult({ query: recallQuery.trim(), result: '' })
    } finally {
      setRecalling(false)
    }
  }

  async function handleDismiss(id) {
    if (dismissingId === id) return
    setDismissingId(id)
    try {
      await api.facts.dismiss(id)
      load()
    } catch {
      // swallow — load() will resync state
    } finally {
      setDismissingId(null)
    }
  }

  function handleRecallKey(e) {
    if (e.key === 'Enter') handleRecall()
  }

  return (
    <div style={{
      width: '100%',
      maxWidth: '1000px',
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>

      {/* ------------------------------------------------------------------ */}
      {/* Header                                                               */}
      {/* ------------------------------------------------------------------ */}
      <ScreenHeader section="Facts" title="Fact Store" />

      {/* ------------------------------------------------------------------ */}
      {/* Recall Tester                                                        */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '8px' }}>Recall Tester</Eyebrow>
        <p style={{ fontSize: '12px', color: '#8a96ad', margin: '0 0 12px 0' }}>
          Test what facts a query would surface from memory recall.
        </p>

        {/* Input row */}
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
          <TextInput
            style={{ flex: '1 1 280px' }}
            placeholder="Enter a query to test…"
            value={recallQuery}
            onChange={e => setRecallQuery(e.target.value)}
            onKeyDown={handleRecallKey}
          />
          <PrimaryButton onClick={handleRecall} disabled={recalling || !recallQuery.trim()}>
            {recalling ? 'Testing…' : 'Test recall'}
          </PrimaryButton>
        </div>

        {/* Recall result */}
        {recallResult !== null && (
          <div style={{
            background: 'rgba(47,212,238,0.04)',
            border: '1px solid rgba(47,212,238,0.12)',
            borderRadius: '10px',
            padding: '12px',
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: '12px',
            color: '#94a6c0',
            marginTop: '12px',
            whiteSpace: 'pre-wrap',
          }}>
            {recallResult.result
              ? recallResult.result
              : <span style={{ color: '#5d6982' }}>No facts matched this query.</span>
            }
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Known Facts                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div>
        <div style={{ marginBottom: '12px' }}>
          <Eyebrow>
            Known Facts{' '}
            <span style={{ color: '#465069' }}>({facts?.length || 0} active)</span>
          </Eyebrow>
        </div>

        {/* Loading */}
        {facts === null && (
          <Card>
            <span style={{ fontSize: '12px', color: '#5d6982', fontFamily: "'JetBrains Mono', monospace" }}>
              Loading…
            </span>
          </Card>
        )}

        {/* Empty state */}
        {facts !== null && facts.length === 0 && (
          <Card dashed style={{
            padding: '40px',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '12px',
            textAlign: 'center',
          }}>
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#5d6982" strokeWidth="1.5">
              <circle cx="12" cy="12" r="9"/>
              <path d="M12 8h.01M11 12h1v4h1"/>
            </svg>
            <span style={{ fontSize: '14px', color: '#5d6982' }}>No facts yet</span>
          </Card>
        )}

        {/* Facts list */}
        {facts !== null && facts.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {facts.map((f) => (
              <div
                key={f.id}
                style={{
                  background: 'rgba(255,255,255,0.022)',
                  border: '1px solid rgba(120,160,220,0.08)',
                  borderRadius: '11px',
                  padding: '12px 14px',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  gap: '12px',
                }}
              >
                {/* Left: fact content + meta */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: '#dbe3f0', marginBottom: '4px' }}>
                    {f.subject}{' '}
                    <span style={{ color: '#5d6982', fontWeight: 400 }}>·</span>{' '}
                    {f.predicate}{' '}
                    <span style={{ color: '#5d6982', fontWeight: 400 }}>·</span>{' '}
                    {f.value}
                  </div>
                  <div style={{ fontSize: '11px', color: '#5d6982', display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
                    {f.source && (
                      <span style={{
                        background: 'rgba(255,255,255,0.04)',
                        border: '1px solid rgba(120,160,220,0.12)',
                        borderRadius: '5px',
                        padding: '1px 6px',
                        fontFamily: "'JetBrains Mono', monospace",
                        fontSize: '10px',
                        textTransform: 'uppercase',
                        letterSpacing: '0.06em',
                      }}>
                        {f.source}
                      </span>
                    )}
                    <span>{relativeTime(f.created_at)}</span>
                  </div>
                </div>

                {/* Right: badges + dismiss */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '6px', flexShrink: 0 }}>
                  {/* Confidence badge */}
                  <span style={{
                    fontSize: '11px',
                    fontWeight: 600,
                    fontFamily: "'JetBrains Mono', monospace",
                    color: f.above_floor ? '#2fd4ee' : '#fbbf24',
                    background: f.above_floor ? 'rgba(47,212,238,0.08)' : 'rgba(251,191,36,0.08)',
                    border: `1px solid ${f.above_floor ? 'rgba(47,212,238,0.20)' : 'rgba(251,191,36,0.20)'}`,
                    borderRadius: '6px',
                    padding: '2px 7px',
                  }}>
                    {Math.round((f.effective_confidence ?? 0) * 100)}% eff
                  </span>

                  {/* Below floor badge */}
                  {!f.above_floor && (
                    <span style={{
                      fontSize: '10px',
                      fontFamily: "'JetBrains Mono', monospace",
                      color: '#fbbf24',
                      border: '1px solid rgba(251,191,36,0.30)',
                      borderRadius: '5px',
                      padding: '1px 6px',
                      letterSpacing: '0.05em',
                      textTransform: 'uppercase',
                    }}>
                      Below floor
                    </span>
                  )}

                  {/* Dismiss button */}
                  <button
                    onClick={() => handleDismiss(f.id)}
                    disabled={dismissingId === f.id}
                    style={{
                      fontSize: '12px',
                      color: dismissingId === f.id ? '#5d6982' : '#fb7185',
                      background: 'none',
                      border: 'none',
                      cursor: dismissingId === f.id ? 'not-allowed' : 'pointer',
                      padding: '2px 4px',
                      opacity: dismissingId === f.id ? 0.5 : 1,
                      fontFamily: 'inherit',
                    }}
                  >
                    {dismissingId === f.id ? 'Dismissing…' : 'Dismiss'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
