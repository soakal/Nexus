import { useState, useEffect, useCallback } from 'react'
import { Brain } from 'lucide-react'
import { api } from '../lib/api'

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

function confColor(aboveFloor) {
  return aboveFloor ? 'text-accent-cyan' : 'text-accent-orange'
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Facts() {
  const [facts, setFacts]           = useState(null)
  const [recallQuery, setRecallQuery] = useState('')
  const [recallResult, setRecallResult] = useState(null)
  const [recalling, setRecalling]   = useState(false)
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
    <div className="p-4 md:p-6 max-w-4xl">
      <div className="flex items-center gap-3 mb-6">
        <Brain
          size={22}
          style={{ color: '#00d4ff', filter: 'drop-shadow(0 0 6px rgba(0,212,255,0.7))' }}
        />
        <h1 className="page-header">FACT STORE</h1>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Recall tester                                                        */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">RECALL TESTER</div>

      <div className="hud-panel-sm p-4 mb-6">
        <div className="font-mono text-xs text-text-secondary mb-3">
          Test what facts a query would surface from memory recall.
        </div>
        <div className="flex flex-wrap gap-3 mb-4">
          <input
            type="text"
            value={recallQuery}
            onChange={e => setRecallQuery(e.target.value)}
            onKeyDown={handleRecallKey}
            placeholder="Enter a query to test..."
            className="hud-input flex-1 font-mono min-w-0"
          />
          <button
            onClick={handleRecall}
            disabled={recalling || !recallQuery.trim()}
            className="glow-btn px-4 py-2 text-xs tracking-widest disabled:opacity-40"
            style={{ boxShadow: '0 0 10px rgba(0,212,255,0.35)' }}
          >
            {recalling ? 'TESTING...' : 'TEST RECALL'}
          </button>
        </div>

        {recallResult !== null && (
          <div>
            <div className="hud-label mb-2">
              RESULT FOR: <span className="text-accent-cyan">{recallResult.query}</span>
            </div>
            <div
              className="font-mono text-xs text-text-primary whitespace-pre-wrap p-3 rounded"
              style={{ background: 'rgba(0,212,255,0.04)', border: '1px solid rgba(0,212,255,0.12)' }}
            >
              {recallResult.result
                ? recallResult.result
                : <span className="text-text-secondary opacity-60">NO FACTS MATCHED THIS QUERY</span>
              }
            </div>
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Known facts list                                                     */}
      {/* ------------------------------------------------------------------ */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">
        KNOWN FACTS
        {facts !== null && (
          <span className="ml-3 font-mono text-xs text-text-secondary">
            ({facts.length} active)
          </span>
        )}
      </div>

      <div className="hud-panel-sm p-4">
        {facts === null ? (
          <div className="hud-label animate-pulse">LOADING...</div>
        ) : facts.length === 0 ? (
          <div className="hud-label opacity-40">NO FACTS YET</div>
        ) : (
          <div className="space-y-1">
            {facts.map((f) => (
              <div
                key={f.id}
                className="py-3"
                style={{ borderBottom: '1px solid rgba(0,212,255,0.06)' }}
              >
                {/* Subject / predicate / value */}
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-2">
                  <span className="font-mono text-xs text-accent-cyan font-bold uppercase tracking-wide">
                    {f.subject}
                  </span>
                  <span className="text-text-secondary text-xs">›</span>
                  <span className="font-mono text-xs text-text-secondary uppercase">
                    {f.predicate}
                  </span>
                  <span className="text-text-secondary text-xs">›</span>
                  <span className="font-mono text-xs text-text-primary flex-1 min-w-0">
                    {f.value}
                  </span>
                </div>

                {/* Meta row */}
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  {/* Effective confidence */}
                  <span
                    className={`font-mono text-xs font-bold ${confColor(f.above_floor)}`}
                    title={f.above_floor ? 'Above recall floor' : 'Below recall floor — will not surface'}
                  >
                    {Math.round((f.effective_confidence ?? 0) * 100)}% EFF
                  </span>

                  {/* Floor badge */}
                  {!f.above_floor && (
                    <span
                      className="font-mono text-xs text-accent-orange px-1.5 py-0.5 rounded"
                      style={{ border: '1px solid rgba(255,149,0,0.4)', fontSize: '0.6rem' }}
                    >
                      BELOW FLOOR
                    </span>
                  )}

                  {/* Source chip */}
                  <span
                    className="font-mono text-xs text-text-secondary px-1.5 py-0.5 rounded"
                    style={{ border: '1px solid rgba(255,255,255,0.1)', fontSize: '0.6rem' }}
                  >
                    {(f.source || 'chat').toUpperCase()}
                  </span>

                  {/* Age */}
                  <span className="font-mono text-xs text-text-secondary">
                    {relativeTime(f.created_at)}
                  </span>

                  {/* Dismiss button */}
                  <button
                    onClick={() => handleDismiss(f.id)}
                    disabled={dismissingId === f.id}
                    className="font-mono text-xs text-text-secondary px-3 py-1 rounded ml-auto disabled:opacity-40 hover:text-red-400 transition-colors"
                    style={{ border: '1px solid rgba(255,255,255,0.1)' }}
                  >
                    {dismissingId === f.id ? 'DISMISSING...' : 'DISMISS'}
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
