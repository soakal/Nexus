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
  const [pinningId, setPinningId] = useState(null)
  const [newFact, setNewFact] = useState({ subject: '', predicate: '', value: '' })
  const [adding, setAdding] = useState(false)

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

  async function handleTogglePin(f) {
    if (pinningId === f.id) return
    setPinningId(f.id)
    try {
      await api.facts.pin(f.id, !f.pinned)
      load()
    } catch {
      // swallow — load() will resync state
    } finally {
      setPinningId(null)
    }
  }

  async function handleAdd() {
    const subject = newFact.subject.trim()
    const predicate = newFact.predicate.trim()
    const value = newFact.value.trim()
    if (!subject || !predicate || !value || adding) return
    setAdding(true)
    try {
      await api.facts.create({ subject, predicate, value })
      setNewFact({ subject: '', predicate: '', value: '' })
      load()
    } catch {
      // swallow — load() will resync state
    } finally {
      setAdding(false)
    }
  }

  function handleAddKey(e) {
    if (e.key === 'Enter') handleAdd()
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
        <p style={{ fontSize: '12px', color: '#98958c', margin: '0 0 12px 0' }}>
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
            background: 'rgba(255,138,61,0.04)',
            border: '1px solid rgba(255,138,61,0.12)',
            borderRadius: '6px',
            padding: '12px',
            fontFamily: "'IBM Plex Mono', monospace",
            fontSize: '12px',
            color: '#a6a399',
            marginTop: '12px',
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
          }}>
            {recallResult.result
              ? recallResult.result
              : <span style={{ color: '#7a776d' }}>No facts matched this query.</span>
            }
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Add a Fact                                                           */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <Eyebrow style={{ display: 'block', marginBottom: '8px' }}>Add a Fact</Eyebrow>
        <p style={{ fontSize: '12px', color: '#98958c', margin: '0 0 12px 0' }}>
          Record something directly. Pin it afterwards to exempt it from confidence decay.
        </p>

        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
          <TextInput
            style={{ flex: '1 1 160px' }}
            placeholder="Subject — e.g. Brian"
            value={newFact.subject}
            onChange={e => setNewFact({ ...newFact, subject: e.target.value })}
            onKeyDown={handleAddKey}
          />
          <TextInput
            style={{ flex: '1 1 160px' }}
            placeholder="Predicate — e.g. allergic to"
            value={newFact.predicate}
            onChange={e => setNewFact({ ...newFact, predicate: e.target.value })}
            onKeyDown={handleAddKey}
          />
          <TextInput
            style={{ flex: '1 1 160px' }}
            placeholder="Value — e.g. penicillin"
            value={newFact.value}
            onChange={e => setNewFact({ ...newFact, value: e.target.value })}
            onKeyDown={handleAddKey}
          />
          <PrimaryButton
            onClick={handleAdd}
            disabled={adding || !newFact.subject.trim() || !newFact.predicate.trim() || !newFact.value.trim()}
          >
            {adding ? 'Adding…' : 'Add fact'}
          </PrimaryButton>
        </div>
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Known Facts                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div>
        <div style={{ marginBottom: '12px' }}>
          <Eyebrow>
            Known Facts{' '}
            <span style={{ color: '#57554c' }}>({facts?.length || 0} active)</span>
          </Eyebrow>
        </div>

        {/* Loading */}
        {facts === null && (
          <Card>
            <span style={{ fontSize: '12px', color: '#7a776d', fontFamily: "'IBM Plex Mono', monospace" }}>
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
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#7a776d" strokeWidth="1.5">
              <circle cx="12" cy="12" r="9"/>
              <path d="M12 8h.01M11 12h1v4h1"/>
            </svg>
            <span style={{ fontSize: '14px', color: '#7a776d' }}>No facts yet</span>
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
                  border: '1px solid rgba(180,178,170,0.08)',
                  borderRadius: '6px',
                  padding: '12px 14px',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  gap: '12px',
                }}
              >
                {/* Left: fact content + meta */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: '#ece9e2', marginBottom: '4px', overflowWrap: 'anywhere' }}>
                    {f.subject}{' '}
                    <span style={{ color: '#7a776d', fontWeight: 400 }}>·</span>{' '}
                    {f.predicate}{' '}
                    <span style={{ color: '#7a776d', fontWeight: 400 }}>·</span>{' '}
                    {f.value}
                  </div>
                  <div style={{ fontSize: '11px', color: '#7a776d', display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
                    {f.source && (
                      <span style={{
                        background: 'rgba(255,255,255,0.04)',
                        border: '1px solid rgba(180,178,170,0.12)',
                        borderRadius: '4px',
                        padding: '1px 6px',
                        fontFamily: "'IBM Plex Mono', monospace",
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
                    fontFamily: "'IBM Plex Mono', monospace",
                    color: f.above_floor ? '#ff8a3d' : '#e8c468',
                    background: f.above_floor ? 'rgba(255,138,61,0.08)' : 'rgba(232,196,104,0.08)',
                    border: `1px solid ${f.above_floor ? 'rgba(255,138,61,0.20)' : 'rgba(232,196,104,0.20)'}`,
                    borderRadius: '4px',
                    padding: '2px 7px',
                  }}>
                    {Math.round((f.effective_confidence ?? 0) * 100)}% eff
                  </span>

                  {/* Below floor badge */}
                  {!f.above_floor && (
                    <span style={{
                      fontSize: '10px',
                      fontFamily: "'IBM Plex Mono', monospace",
                      color: '#e8c468',
                      border: '1px solid rgba(232,196,104,0.30)',
                      borderRadius: '4px',
                      padding: '1px 6px',
                      letterSpacing: '0.05em',
                      textTransform: 'uppercase',
                    }}>
                      Below floor
                    </span>
                  )}

                  {/* Pinned badge */}
                  {f.pinned && (
                    <span style={{
                      fontSize: '10px',
                      fontFamily: "'IBM Plex Mono', monospace",
                      color: '#ff8a3d',
                      border: '1px solid rgba(255,138,61,0.30)',
                      borderRadius: '4px',
                      padding: '1px 6px',
                      letterSpacing: '0.05em',
                      textTransform: 'uppercase',
                    }}>
                      Pinned
                    </span>
                  )}

                  {/* Pin toggle */}
                  <button
                    onClick={() => handleTogglePin(f)}
                    disabled={pinningId === f.id}
                    style={{
                      fontSize: '12px',
                      color: f.pinned ? '#ff8a3d' : '#7a776d',
                      background: 'none',
                      border: 'none',
                      cursor: pinningId === f.id ? 'not-allowed' : 'pointer',
                      padding: '8px 10px',
                      opacity: pinningId === f.id ? 0.5 : 1,
                      fontFamily: 'inherit',
                    }}
                  >
                    {pinningId === f.id ? 'Saving…' : f.pinned ? 'Unpin' : 'Pin'}
                  </button>

                  {/* Dismiss button */}
                  <button
                    onClick={() => handleDismiss(f.id)}
                    disabled={dismissingId === f.id}
                    style={{
                      fontSize: '12px',
                      color: dismissingId === f.id ? '#7a776d' : '#fb7185',
                      background: 'none',
                      border: 'none',
                      cursor: dismissingId === f.id ? 'not-allowed' : 'pointer',
                      padding: '8px 10px',
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
