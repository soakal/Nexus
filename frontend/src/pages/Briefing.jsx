import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import BriefingPanel from '../components/BriefingPanel'
import { fmtDateTime } from '../lib/parseUTC'

export default function Briefing() {
  const [briefing, setBriefing] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.briefing.latest().then(b => setBriefing(b)).catch(() => {})
  }, [])

  const trigger = async () => {
    setLoading(true)
    try {
      const r = await api.briefing.trigger()
      setBriefing({ content: r.briefing, created_at: new Date().toISOString() })
    } catch {}
    setLoading(false)
  }

  const created = briefing ? fmtDateTime(briefing.created_at) : ''

  return (
    <div className="p-4 md:p-6 max-w-3xl">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <h1 className="page-header">INTEL BRIEFING</h1>
        <button onClick={trigger} disabled={loading} className="glow-btn disabled:opacity-50">
          {loading ? 'GENERATING...' : 'GENERATE'}
        </button>
      </div>
      {briefing && (
        <div className="mb-4">
          <span className="hud-label">TIMESTAMP </span>
          <span className="font-mono text-text-secondary text-xs">
            {created}
          </span>
        </div>
      )}
      <BriefingPanel content={briefing?.content} />
    </div>
  )
}
