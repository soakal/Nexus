import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'
import { parseUTC } from '../lib/parseUTC'
import RecordingCard from '../components/RecordingCard'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'

function formatDateTime(iso) {
  if (!iso) return null
  const d = parseUTC(iso)
  if (isNaN(d.getTime())) return null
  return d.toLocaleString([], {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export default function Media() {
  const [data, setData] = useState(null)
  const [scheduling, setScheduling] = useState({})
  const unmountedRef = useRef(false)

  const load = () => {
    api.channels.get().then(d => {
      if (!unmountedRef.current) setData(d)
    }).catch(() => {})
  }

  useEffect(() => {
    unmountedRef.current = false
    load()
    const timer = setInterval(load, 30000)
    const onVis = () => { if (!document.hidden && !unmountedRef.current) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      unmountedRef.current = true
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [])

  const schedule = async (programId) => {
    setScheduling(prev => ({ ...prev, [programId]: 'scheduling' }))
    try {
      await api.channels.record(programId)
      if (unmountedRef.current) return
      setScheduling(prev => ({ ...prev, [programId]: 'scheduled' }))
      load()
      setTimeout(() => {
        setScheduling(prev => {
          const next = { ...prev }
          delete next[programId]
          return next
        })
      }, 2000)
    } catch {
      if (unmountedRef.current) return
      setScheduling(prev => ({ ...prev, [programId]: 'error' }))
    }
  }

  const pct = data?.storage_total_gb > 0
    ? Math.round(data.storage_used_gb / data.storage_total_gb * 100)
    : 0

  return (
    <div style={{
      width: '100%',
      maxWidth: '1100px',
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Media" title="Media Operations" />

      {!data && (
        <div style={{ color: '#5d6982', fontSize: '13px' }}>Loading…</div>
      )}

      {/* Top row: Now Recording + Upcoming */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--gap)' }}>

        {/* Now Recording */}
        <Card style={{ flex: '1 1 300px' }}>
          <Eyebrow style={{ marginBottom: '14px' }}>Now Recording</Eyebrow>
          {data?.recording_now?.length > 0
            ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {data.recording_now.map((r, i) => <RecordingCard key={i} recording={r} />)}
              </div>
            )
            : (
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', color: '#8a96ad' }}>
                <StatusDot color="#7c8aa3" size={8} glow={false} />
                <span style={{ fontSize: '14px' }}>Nothing recording</span>
              </div>
            )
          }
        </Card>

        {/* Upcoming */}
        <Card style={{ flex: '1 1 320px' }}>
          <Eyebrow style={{ marginBottom: '14px' }}>Upcoming</Eyebrow>
          {(data?.upcoming || []).length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {(data.upcoming || []).map((r, i) => {
                const startLabel = formatDateTime(r.start)
                const schedState = r.program_id ? scheduling[r.program_id] : undefined

                let recLabel = 'REC'
                let recStyle = {
                  display: 'flex',
                  alignItems: 'center',
                  gap: '5px',
                  fontSize: '11px',
                  fontWeight: 700,
                  letterSpacing: '0.08em',
                  color: '#fb7185',
                  padding: '3px 9px',
                  borderRadius: '6px',
                  border: '1px solid rgba(251,113,133,0.3)',
                  background: 'transparent',
                  cursor: 'pointer',
                  whiteSpace: 'nowrap',
                }
                if (schedState === 'scheduling') {
                  recLabel = '...'
                  recStyle = { ...recStyle, opacity: 0.6, cursor: 'not-allowed' }
                } else if (schedState === 'scheduled') {
                  recLabel = 'SCHEDULED'
                  recStyle = {
                    ...recStyle,
                    color: 'rgba(0,212,255,1)',
                    border: '1px solid rgba(0,212,255,0.8)',
                    cursor: 'not-allowed',
                  }
                } else if (schedState === 'error') {
                  recLabel = 'ERROR'
                  recStyle = {
                    ...recStyle,
                    color: 'rgba(255,120,0,0.9)',
                    border: '1px solid rgba(255,120,0,0.6)',
                    cursor: 'not-allowed',
                  }
                }

                return (
                  <div
                    key={i}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: '12px',
                      flexWrap: 'wrap',
                      padding: '13px 14px',
                      borderRadius: '11px',
                      background: 'rgba(255,255,255,0.022)',
                      border: '1px solid rgba(120,160,220,0.08)',
                    }}
                  >
                    {/* Left: title + channel chip */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                      <span style={{
                        fontSize: '14px',
                        fontWeight: 600,
                        color: '#dbe3f0',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}>
                        {r.title}
                      </span>
                      {r.channel && (
                        <span style={{
                          fontFamily: "'JetBrains Mono', monospace",
                          fontSize: '11px',
                          color: 'var(--accent)',
                          padding: '3px 8px',
                          border: '1px solid var(--ac-line)',
                          borderRadius: '6px',
                          whiteSpace: 'nowrap',
                          flexShrink: 0,
                        }}>
                          CH {r.channel}
                        </span>
                      )}
                    </div>

                    {/* Right: start time + badge */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                      {startLabel && (
                        <span style={{ fontSize: '12px', color: '#8a96ad', whiteSpace: 'nowrap' }}>
                          {startLabel}
                        </span>
                      )}
                      {r.program_id && (
                        <button
                          onClick={() => schedule(r.program_id)}
                          disabled={!!schedState}
                          style={recStyle}
                        >
                          {/* red dot indicator */}
                          <span style={{
                            display: 'inline-block',
                            width: '6px',
                            height: '6px',
                            borderRadius: '50%',
                            background: schedState === 'scheduled'
                              ? 'rgba(0,212,255,1)'
                              : schedState === 'error'
                                ? 'rgba(255,120,0,0.9)'
                                : '#fb7185',
                            flexShrink: 0,
                          }} />
                          {recLabel}
                        </button>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            <div style={{ color: '#5d6982', fontSize: '13px' }}>No upcoming recordings</div>
          )}
        </Card>
      </div>

      {/* Storage — full width */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
          <Eyebrow>Storage</Eyebrow>
          <span style={{ fontSize: '13px', color: '#8a96ad' }}>
            {(data?.storage_used_gb / 1000 || 0).toFixed(1)} GB of {(data?.storage_total_gb / 1000 || 0).toFixed(1)} GB
            {' · '}
            <strong style={{ color: '#5b8cff' }}>{pct}%</strong>
          </span>
        </div>
        <div style={{
          height: '10px',
          borderRadius: '6px',
          background: 'rgba(120,160,220,0.12)',
          overflow: 'hidden',
        }}>
          <div style={{
            width: `${pct}%`,
            height: '100%',
            background: 'linear-gradient(90deg,#5b8cff,#2fd4ee)',
            borderRadius: '6px',
            transition: 'width 0.4s ease',
          }} />
        </div>
      </Card>
    </div>
  )
}
