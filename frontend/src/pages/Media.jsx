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

  // Periodic/mount/focus reads use the cached snapshot (dashboard.channels,
  // same channels_dvr.fetch() call, refreshed every 60s by a background
  // collector) instead of a live call on every 30s poll -- same pattern as
  // the Uptime.jsx and Dashboard.jsx fixes.
  const load = () => {
    api.dashboard.state().then(d => {
      if (!unmountedRef.current && d?.channels?.data) setData(d.channels.data)
    }).catch(() => {})
  }

  // A just-scheduled recording needs to show up immediately, not wait for
  // the next collector tick -- this one path stays a live call on purpose.
  const loadLive = () => {
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
      loadLive()
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
        <div style={{ color: '#7a776d', fontSize: '13px' }}>Loading…</div>
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
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', color: '#98958c' }}>
                <StatusDot color="#8b8880" size={8} glow={false} />
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
                  borderRadius: '4px',
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
                    color: 'rgba(255,138,61,1)',
                    border: '1px solid rgba(255,138,61,0.8)',
                    cursor: 'not-allowed',
                  }
                } else if (schedState === 'error') {
                  recLabel = 'ERROR'
                  recStyle = {
                    ...recStyle,
                    color: 'rgba(232,196,104,0.9)',
                    border: '1px solid rgba(232,196,104,0.6)',
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
                      borderRadius: '6px',
                      background: 'rgba(255,255,255,0.022)',
                      border: '1px solid rgba(180,178,170,0.08)',
                    }}
                  >
                    {/* Left: title + channel chip */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                      <span style={{
                        fontSize: '14px',
                        fontWeight: 600,
                        color: '#ece9e2',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}>
                        {r.title}
                      </span>
                      {r.channel && (
                        <span style={{
                          fontFamily: "'IBM Plex Mono', monospace",
                          fontSize: '11px',
                          color: 'var(--accent)',
                          padding: '3px 8px',
                          border: '1px solid var(--ac-line)',
                          borderRadius: '4px',
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
                        <span style={{ fontSize: '12px', color: '#98958c', whiteSpace: 'nowrap' }}>
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
                              ? 'rgba(255,138,61,1)'
                              : schedState === 'error'
                                ? 'rgba(232,196,104,0.9)'
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
            <div style={{ color: '#7a776d', fontSize: '13px' }}>No upcoming recordings</div>
          )}
        </Card>
      </div>

      {/* Storage — full width */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
          <Eyebrow>Storage</Eyebrow>
          <span style={{ fontSize: '13px', color: '#98958c' }}>
            {(data?.storage_used_gb / 1000 || 0).toFixed(1)} GB of {(data?.storage_total_gb / 1000 || 0).toFixed(1)} GB
            {' · '}
            <strong style={{ color: '#ff8a3d' }}>{pct}%</strong>
          </span>
        </div>
        <div style={{
          height: '10px',
          borderRadius: '4px',
          background: 'rgba(180,178,170,0.12)',
          overflow: 'hidden',
        }}>
          <div style={{
            width: `${pct}%`,
            height: '100%',
            background: 'linear-gradient(90deg,#ff8a3d,#ff8a3d)',
            borderRadius: '4px',
            transition: 'width 0.4s ease',
          }} />
        </div>
      </Card>
    </div>
  )
}
