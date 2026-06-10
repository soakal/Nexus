import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import RunHistory from '../components/RunHistory'
import AgentLog from '../components/AgentLog'
export default function Agents() {
  const [runs, setRuns] = useState([])
  const [q, setQ] = useState('')
  useEffect(() => { api.agents.runs(q).then(setRuns).catch(() => {}) }, [q])
  return (
    <div className="p-6 max-w-3xl space-y-6">
      <h1 className="page-header">AGENT COMMAND CENTER</h1>
      <div>
        <div className="flex items-center gap-2 mb-2">
          <span className="arc-dot" />
          <span className="hud-label">LIVE FEED</span>
        </div>
        <AgentLog />
      </div>
      <div>
        <input value={q} onChange={e => setQ(e.target.value)}
          className="hud-input w-full"
          placeholder="SEARCH RUN HISTORY..." />
      </div>
      <div>
        <div className="flex items-center gap-2 mb-2">
          <span className="hud-label">RUN HISTORY</span>
        </div>
        <RunHistory runs={runs} />
      </div>
    </div>
  )
}
