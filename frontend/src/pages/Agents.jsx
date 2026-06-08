import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import RunHistory from '../components/RunHistory'
import AgentLog from '../components/AgentLog'
export default function Agents() {
  const [runs, setRuns] = useState([])
  const [q, setQ] = useState('')
  useEffect(() => { api.agents.runs(q).then(setRuns).catch(() => {}) }, [q])
  return (
    <div className="p-6 max-w-3xl">
      <h1 className="font-mono text-accent-cyan text-xl font-bold mb-6">AGENT COMMAND CENTER</h1>
      <div className="mb-4">
        <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-2">Live Log</h2>
        <AgentLog />
      </div>
      <div className="mb-4">
        <input value={q} onChange={e => setQ(e.target.value)}
          className="w-full bg-bg-card border border-border-dark rounded px-3 py-2 text-text-primary text-sm placeholder-text-secondary"
          placeholder="Search run history..." />
      </div>
      <RunHistory runs={runs} />
    </div>
  )
}
