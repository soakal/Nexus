import { useState } from 'react'
import { api } from '../lib/api'
import Card from './Card'
import Eyebrow from './Eyebrow'
import StatusPill from './StatusPill'
import GhostButton from './GhostButton'

export default function BrainOrganizerCard({ data, onRun, style = {} }) {
  const [triggering, setTriggering] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [triggerError, setTriggerError] = useState(null)

  const handleRun = async () => {
    setTriggerError(null)
    setTriggering(true)
    try {
      await api.brain.run()
      onRun?.()
    } catch (e) {
      setTriggerError(e.message?.includes('409') ? 'ALREADY RUNNING' : 'TRIGGER FAILED')
    } finally {
      setTriggering(false)
    }
  }

  const handleReset = async () => {
    setTriggerError(null)
    setResetting(true)
    try {
      const r = await api.brain.resetFailed()
      onRun?.()
      setTriggerError(`RESET ${r.reset} FAILED`)
    } catch (e) {
      setTriggerError('RESET FAILED')
    } finally {
      setResetting(false)
    }
  }

  const lastRun = data?.last_run
    ? new Date(data.last_run).toLocaleString()
    : '—'

  return (
    <Card style={style}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', flexWrap: 'wrap' }}>
        <Eyebrow>Brain Organizer</Eyebrow>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <StatusPill
            tone={data?.running ? 'accent' : 'grey'}
            dotPulse={data?.running}
            label={data?.running ? 'Running' : 'Idle'}
          />
          {data?.failed > 0 && (
            <GhostButton
              onClick={handleReset}
              disabled={data?.running || resetting}
              style={{ color: '#fb7185', borderColor: 'rgba(251,113,133,0.3)' }}
              icon={
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                  <path d="M3 3v5h5"/>
                </svg>
              }
            >
              {resetting ? 'Resetting…' : `Reset ${data.failed} failed`}
            </GhostButton>
          )}
          <GhostButton
            onClick={handleRun}
            disabled={data?.running || triggering}
            icon={
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M5 3l14 9-14 9z"/>
              </svg>
            }
          >
            {triggering ? 'Starting…' : 'Run now'}
          </GhostButton>
        </div>
      </div>

      {/* Stat tiles */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(130px,1fr))', gap: '12px', marginTop: '18px' }}>
        {/* Processed */}
        <div style={{ padding: '16px', borderRadius: '12px', background: 'rgba(52,211,153,0.07)', border: '1px solid rgba(52,211,153,0.18)' }}>
          <div style={{ fontSize: '30px', fontWeight: 700, color: '#34d399' }}>{data?.succeeded || 0}</div>
          <div style={{ fontSize: '11px', letterSpacing: '0.1em', textTransform: 'uppercase', color: '#5fe0b4', fontWeight: 600, marginTop: '4px' }}>Processed</div>
        </div>
        {/* Pending */}
        <div style={{ padding: '16px', borderRadius: '12px', background: 'rgba(251,191,36,0.07)', border: '1px solid rgba(251,191,36,0.18)' }}>
          <div style={{ fontSize: '30px', fontWeight: 700, color: '#fbbf24' }}>{data?.pending || 0}</div>
          <div style={{ fontSize: '11px', letterSpacing: '0.1em', textTransform: 'uppercase', color: '#fbbf24', fontWeight: 600, marginTop: '4px' }}>Pending</div>
        </div>
        {/* Failed */}
        <div style={{ padding: '16px', borderRadius: '12px', background: 'rgba(251,113,133,0.07)', border: '1px solid rgba(251,113,133,0.18)' }}>
          <div style={{ fontSize: '30px', fontWeight: 700, color: '#fb7185' }}>{data?.failed || 0}</div>
          <div style={{ fontSize: '11px', letterSpacing: '0.1em', textTransform: 'uppercase', color: '#fb7185', fontWeight: 600, marginTop: '4px' }}>Failed</div>
        </div>
      </div>

      {/* Meta line */}
      <div style={{ fontSize: '12px', color: '#5d6982', marginTop: '14px' }}>
        Last run {lastRun} · Scheduled daily 2:00 AM
      </div>

      {/* Console log */}
      <div style={{ background: '#070a11', border: '1px solid rgba(120,160,220,0.10)', borderRadius: '12px', padding: '14px 16px', maxHeight: '172px', overflow: 'auto', marginTop: '14px' }}>
        {(data?.log_tail || []).map((line, i) => {
          const m = line.match(/\[([A-Z]+)\](.*)/)
          const tag = m ? m[1] : null
          const msg = m ? m[2] : line
          const tagColor = !tag ? 'var(--accent)'
            : tag === 'ERROR' ? '#fb7185'
            : tag === 'WARNING' ? '#fbbf24'
            : 'var(--accent)'
          return (
            <div key={i} style={{ display: 'flex', gap: '10px', fontFamily: "'JetBrains Mono',monospace", fontSize: '12px', lineHeight: 1.85 }}>
              {tag && (
                <span style={{ flex: 'none', fontWeight: 500, color: tagColor }}>[{tag}]</span>
              )}
              <span style={{ color: '#94a6c0', wordBreak: 'break-word' }}>{msg}</span>
            </div>
          )
        })}
      </div>

      {/* Trigger error */}
      {triggerError && (
        <div style={{ fontSize: '12px', color: '#fb7185', marginTop: '10px' }}>{triggerError}</div>
      )}
    </Card>
  )
}
