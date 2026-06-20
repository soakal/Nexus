import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import RunHistory from '../components/RunHistory'
import AgentLog from '../components/AgentLog'
import ScreenHeader from '../components/ScreenHeader'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import TextInput from '../components/TextInput'

export default function Agents() {
  const [runs, setRuns] = useState([])
  const [q, setQ] = useState('')
  useEffect(() => { api.agents.runs(q).then(setRuns).catch(() => {}) }, [q])

  const filteredRuns = runs

  return (
    <div style={{
      width: '100%',
      maxWidth: 1100,
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Agents" title="Agent Command Center" />

      {/* Live Feed Card */}
      <div style={{
        background: 'linear-gradient(180deg,rgba(255,255,255,0.025),rgba(255,255,255,0)),#0c1320',
        border: '1px solid rgba(120,160,220,0.10)',
        borderRadius: 14,
        padding: '18px 20px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
          <StatusDot color="#34d399" size={7} pulse />
          <Eyebrow>Live Feed</Eyebrow>
        </div>
        <AgentLog />
      </div>

      {/* Search */}
      <TextInput
        style={{ width: '100%' }}
        placeholder="Search run history…"
        value={q}
        onChange={e => setQ(e.target.value)}
      />

      {/* Run History */}
      <div>
        <div style={{ marginBottom: 12 }}>
          <Eyebrow>Run History</Eyebrow>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <RunHistory runs={filteredRuns} />
        </div>
      </div>
    </div>
  )
}
