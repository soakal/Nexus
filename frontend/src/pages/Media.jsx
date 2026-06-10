import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'
import { parseUTC } from '../lib/parseUTC'
import RecordingCard from '../components/RecordingCard'

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
    return () => {
      unmountedRef.current = true
      clearInterval(timer)
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

  return (
    <div className="p-6 max-w-2xl">
      <h1 className="page-header mb-6">MEDIA OPERATIONS</h1>
      {!data ? (
        <div className="hud-label animate-pulse">LOADING...</div>
      ) : (
        <div className="space-y-6">

          {/* Now Recording */}
          <div>
            <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">NOW RECORDING</div>
            {data.recording_now?.length > 0
              ? <div className="space-y-2">{data.recording_now.map((r, i) => <RecordingCard key={i} recording={r} />)}</div>
              : <div className="hud-label opacity-40">NOTHING RECORDING</div>}
          </div>

          {/* Upcoming */}
          <div>
            <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">UPCOMING</div>
            {(data.upcoming || []).length > 0 ? (
              <div className="space-y-2">
                {data.upcoming.map((r, i) => {
                  const startLabel = formatDateTime(r.start)
                  const schedState = r.program_id ? scheduling[r.program_id] : undefined

                  let recLabel = 'REC'
                  let recStyle = {}
                  if (schedState === 'scheduling') {
                    recLabel = '...'
                    recStyle = { opacity: 0.6 }
                  } else if (schedState === 'scheduled') {
                    recLabel = 'SCHEDULED'
                    recStyle = { color: 'rgba(0,212,255,1)', borderColor: 'rgba(0,212,255,0.8)' }
                  } else if (schedState === 'error') {
                    recLabel = 'ERROR'
                    recStyle = { color: 'rgba(255,120,0,0.9)', borderColor: 'rgba(255,120,0,0.6)' }
                  }

                  return (
                    <div key={i} className="hud-panel-sm p-3 flex items-start justify-between gap-3 min-w-0">
                      <span className="text-text-primary text-sm truncate flex-1 leading-snug">{r.title}</span>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        {r.channel && (
                          <span className="inline-block bg-bg-secondary border border-accent-cyan text-accent-cyan font-mono text-xs px-1.5 py-0.5 leading-none tracking-wider">
                            CH {r.channel}
                          </span>
                        )}
                        {startLabel && (
                          <span className="hud-label text-xs whitespace-nowrap">{startLabel}</span>
                        )}
                        {r.program_id && (
                          <button
                            onClick={() => schedule(r.program_id)}
                            disabled={!!schedState}
                            className="glow-btn"
                            style={{
                              fontSize: '0.6rem',
                              padding: '2px 6px',
                              opacity: schedState ? 0.7 : 1,
                              cursor: schedState ? 'not-allowed' : 'pointer',
                              whiteSpace: 'nowrap',
                              ...recStyle,
                            }}
                          >
                            {recLabel}
                          </button>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="hud-label opacity-40">NO UPCOMING RECORDINGS</div>
            )}
          </div>

          {/* Storage */}
          {data.storage_total_gb > 0 ? (
            <div className="hud-panel p-4">
              <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">STORAGE</div>
              <div className="flex items-center justify-between mb-1">
                <span className="hud-label">{data.storage_used_gb}GB used of {data.storage_total_gb}GB</span>
                <span className="hud-label">{Math.round(data.storage_used_gb / data.storage_total_gb * 100)}%</span>
              </div>
              <div className="w-full bg-bg-secondary border border-border-dark h-3">
                <div
                  className="bg-accent-cyan h-full"
                  style={{
                    width: `${data.storage_used_gb / data.storage_total_gb * 100}%`,
                    boxShadow: '0 0 12px rgba(0,212,255,0.5)',
                  }}
                />
              </div>
            </div>
          ) : (
            <div className="hud-panel p-4">
              <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-1">STORAGE</div>
              <div className="hud-label opacity-40 mt-2">STORAGE DATA UNAVAILABLE</div>
            </div>
          )}

        </div>
      )}
    </div>
  )
}
