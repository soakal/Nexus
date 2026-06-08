import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import BriefingPanel from '../components/BriefingPanel'
export default function Briefing() {
  const [briefing, setBriefing] = useState(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => { api.briefing.latest().then(b => setBriefing(b)).catch(() => {}) }, [])
  const trigger = async () => { setLoading(true); try { const r = await api.briefing.trigger(); setBriefing({ content: r.briefing, created_at: new Date().toISOString() }) } catch {} setLoading(false) }
  return (
    <div className="p-6 max-w-3xl">
      <div className="flex items-center justify-between mb-6">
        <h1 className="font-mono text-accent-cyan text-xl font-bold">MORNING BRIEF</h1>
        <button onClick={trigger} disabled={loading} className="bg-accent-cyan text-bg-primary font-mono text-sm px-4 py-2 rounded font-bold hover:opacity-90 disabled:opacity-50">
          {loading ? 'GENERATING...' : 'GENERATE'}
        </button>
      </div>
      {briefing && <div className="text-text-secondary text-xs mb-4">{new Date(briefing.created_at.endsWith('Z') ? briefing.created_at : briefing.created_at + 'Z').toLocaleString()}</div>}
      <BriefingPanel content={briefing?.content} />
    </div>
  )
}

