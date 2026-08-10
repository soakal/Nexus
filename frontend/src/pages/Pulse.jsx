import { useState, useEffect, useRef } from 'react'
import { api, wsActivityUrl, wsActivityProtocols } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'

// Same naive-UTC convention as Traces.jsx (no trailing 'Z' from the backend).
function toMs(iso) {
  if (!iso) return null
  const t = new Date(iso.endsWith('Z') ? iso : iso + 'Z').getTime()
  return Number.isNaN(t) ? null : t
}

function relativeTime(iso) {
  const t = toMs(iso)
  if (t === null) return '—'
  const diff = Math.floor((Date.now() - t) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`
}

function fmtElapsed(startedAtIso, nowTick) {
  const t = toMs(startedAtIso)
  if (t === null) return ''
  const s = Math.max(0, Math.floor((nowTick - t) / 1000))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

function statusColor(status) {
  switch (status) {
    case 'running': return '#2fd4ee'
    case 'ok': return '#5fe0b4'
    case 'error': return '#fb7185'
    default: return '#5d6982'
  }
}

const ACTOR_GROUP_LABELS = { worker: 'Workers', loop: 'Loops', job: 'Scheduled jobs', task: 'Tasks' }
const GROUP_ORDER = ['worker', 'loop', 'job', 'task']

export default function Pulse() {
  const [entries, setEntries] = useState({})   // actor_id -> entry
  const [events, setEvents] = useState([])     // oldest-first, capped at 200
  const [connected, setConnected] = useState(false)
  const [autonomy, setAutonomy] = useState(null)
  const [nowTick, setNowTick] = useState(Date.now())
  const [paused, setPaused] = useState(false)
  const pausedRef = useRef(false)
  const pendingEventsRef = useRef([])

  useEffect(() => {
    const t = setInterval(() => setNowTick(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    pausedRef.current = paused
    if (!paused && pendingEventsRef.current.length) {
      const buffered = pendingEventsRef.current
      pendingEventsRef.current = []
      setEvents(prev => [...prev, ...buffered].slice(-200))
    }
  }, [paused])

  useEffect(() => {
    const loadAutonomy = () => api.safety.status().then(setAutonomy).catch(() => {})
    loadAutonomy()
    const t = setInterval(loadAutonomy, 30000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    // Initial REST paint before the socket opens.
    api.activity.snapshot().then(snap => {
      const map = {}
      for (const e of (snap.entries || [])) map[e.actor_id] = e
      setEntries(map)
      setEvents(snap.events || [])
    }).catch(() => {})
  }, [])

  useEffect(() => {
    let stopped = false
    let socket = null
    let reconnectTimer = null

    const applyEntries = (list) => {
      if (!list || !list.length) return
      setEntries(prev => {
        const next = { ...prev }
        for (const e of list) next[e.actor_id] = e
        return next
      })
    }
    // Server-side remove()/sweep_stale() drop an entry from the registry
    // immediately, but a connected client only learns about it via this
    // channel on the NEXT delta -- without applying it, a finished task
    // would show as permanently "running" until the next reconnect/snapshot.
    const applyRemoved = (ids) => {
      if (!ids || !ids.length) return
      setEntries(prev => {
        const next = { ...prev }
        for (const id of ids) delete next[id]
        return next
      })
    }
    const applyEvents = (list) => {
      if (!list || !list.length) return
      if (pausedRef.current) {
        // Capped the same way the server-side ring is -- a cursor parked
        // over the ticker for a long time must not grow this unbounded.
        pendingEventsRef.current = [...pendingEventsRef.current, ...list].slice(-200)
        return
      }
      setEvents(prev => [...prev, ...list].slice(-200))
    }

    const connect = () => {
      if (stopped) return
      try {
        socket = new WebSocket(wsActivityUrl(), wsActivityProtocols())
        socket.onopen = () => setConnected(true)
        socket.onmessage = event => {
          try {
            const msg = JSON.parse(event.data)
            if (msg.type === 'activity.snapshot') {
              const map = {}
              for (const e of (msg.entries || [])) map[e.actor_id] = e
              setEntries(map)
              setEvents(msg.events || [])
            } else if (msg.type === 'activity.delta') {
              applyEntries(msg.entries)
              applyRemoved(msg.removed)
              applyEvents(msg.events)
            }
          } catch {}
        }
        socket.onclose = () => {
          setConnected(false)
          if (!stopped) reconnectTimer = setTimeout(connect, 2000)
        }
        socket.onerror = () => socket?.close()
      } catch {
        reconnectTimer = setTimeout(connect, 2000)
      }
    }

    connect()
    return () => {
      stopped = true
      clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [])

  const allEntries = Object.values(entries)
  const running = allEntries
    .filter(e => e.status === 'running' && ['task', 'trace', 'worker'].includes(e.actor_type))
    .sort((a, b) => (b.seq || 0) - (a.seq || 0))

  const grouped = {}
  for (const e of allEntries) {
    if (e.actor_type === 'trace') continue  // no dedicated board card — surfaced via "Now Running" only
    const key = e.actor_type
    if (!grouped[key]) grouped[key] = []
    grouped[key].push(e)
  }
  for (const key of Object.keys(grouped)) {
    grouped[key].sort((a, b) => {
      if (a.status === 'running' && b.status !== 'running') return -1
      if (b.status === 'running' && a.status !== 'running') return 1
      return (toMs(b.last_run_at) || 0) - (toMs(a.last_run_at) || 0)
    })
  }
  const groupKeys = GROUP_ORDER.filter(k => grouped[k]?.length)

  const tickerRows = [...events].reverse()

  return (
    <div style={{
      width: '100%', maxWidth: '1100px', margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex', flexDirection: 'column', gap: 'var(--gap)',
    }}>
      <ScreenHeader
        section="Systems"
        title="Pulse"
        subline="What NEXUS is doing right now"
        right={
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <StatusDot color={connected ? '#5fe0b4' : '#fb7185'} pulse={connected} />
            <span style={{ fontSize: '12px', color: '#8a96ad' }}>{connected ? 'live' : 'reconnecting…'}</span>
            {autonomy && (
              <span style={{ fontSize: '12px', color: '#8a96ad' }}>
                · autonomy {autonomy.autonomy_enabled ? 'ON' : 'PAUSED'} · ${Number(autonomy.today_spend_usd || 0).toFixed(2)} today
              </span>
            )}
          </div>
        }
      />

      <Card>
        <Eyebrow style={{ marginBottom: '10px' }}>Now Running</Eyebrow>
        {running.length === 0 ? (
          <div style={{ fontSize: '13px', color: '#5d6982' }}>
            Nothing in flight — {grouped.worker?.length || 0} worker(s) idle.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {running.map(e => (
              <div key={e.actor_id} style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <StatusDot color="#2fd4ee" pulse size={8} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {e.label || e.actor_id}
                  </div>
                  {e.actor_type === 'task' && e.detail?.total_steps > 0 ? (
                    <div style={{ marginTop: '4px' }}>
                      <div style={{ fontSize: '11px', color: '#8a96ad', marginBottom: '3px' }}>
                        step {e.detail.step_index}/{e.detail.total_steps} — {e.detail.description}
                      </div>
                      <div style={{ height: '4px', borderRadius: '2px', background: 'rgba(120,160,220,0.12)', overflow: 'hidden' }}>
                        <div style={{
                          height: '100%',
                          width: `${Math.min(100, (e.detail.step_index / e.detail.total_steps) * 100)}%`,
                          background: '#2fd4ee',
                        }} />
                      </div>
                    </div>
                  ) : null}
                </div>
                <div style={{ fontSize: '12px', color: '#8a96ad', fontVariantNumeric: 'tabular-nums', flex: 'none' }}>
                  {fmtElapsed(e.started_at, nowTick)}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <Eyebrow style={{ marginBottom: '10px' }}>Actors</Eyebrow>
        {groupKeys.length === 0 ? (
          <div style={{ fontSize: '13px', color: '#5d6982' }}>No data yet — waiting for the first tick.</div>
        ) : (
          groupKeys.map(type => (
            <div key={type} style={{ marginBottom: '18px' }}>
              <div style={{ fontSize: '11px', fontWeight: 700, color: '#5d6982', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '8px' }}>
                {ACTOR_GROUP_LABELS[type] || type}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: '10px' }}>
                {grouped[type].map(e => (
                  <div key={e.actor_id} style={{
                    padding: '10px 12px', borderRadius: '10px',
                    background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(120,160,220,0.10)',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                      <StatusDot color={statusColor(e.status)} pulse={e.status === 'running'} size={7} />
                      <span style={{ fontSize: '12px', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {e.actor_id.replace(/^(job|worker|loop):/, '')}
                      </span>
                    </div>
                    <div style={{ fontSize: '11px', color: '#8a96ad' }}>
                      {e.status === 'running'
                        ? `running · ${fmtElapsed(e.started_at, nowTick)}`
                        : e.last_run_at
                          ? `last ran ${relativeTime(e.last_run_at)} · ${fmtMs(e.last_duration_ms)} · ${e.status === 'error' ? 'ERROR' : 'OK'}`
                          : 'no data yet'}
                    </div>
                    {e.status === 'error' && e.last_error ? (
                      <div style={{ fontSize: '11px', color: '#fb7185', marginTop: '3px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {e.last_error}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          ))
        )}
      </Card>

      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
          <Eyebrow>Live Ticker</Eyebrow>
          {paused && <span style={{ fontSize: '11px', color: '#8a96ad' }}>paused — hover to pin, move away to resume</span>}
        </div>
        {/* Rows below need flexShrink:0 -- a column flex container with
            maxHeight + overflow:hidden children lets the browser shrink
            every child toward 0 height to "fit" instead of overflowing and
            scrolling, since overflow:hidden resets a flex item's automatic
            min-height to 0. Found live: real ticker data was in the DOM
            with correct text/color but rendered at height:0px, invisible. */}
        <div
          onMouseEnter={() => setPaused(true)}
          onMouseLeave={() => setPaused(false)}
          style={{
            maxHeight: '360px', overflowY: 'auto', fontFamily: 'ui-monospace, monospace', fontSize: '12px',
            display: 'flex', flexDirection: 'column', gap: '4px',
          }}
        >
          {tickerRows.length === 0 ? (
            <div style={{ color: '#5d6982' }}>No activity yet.</div>
          ) : (
            tickerRows.map((ev, i) => (
              <div key={`${ev.ts}-${i}`} style={{ color: '#c8d0e0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', flexShrink: 0 }}>
                <span style={{ color: '#5d6982' }}>{new Date(ev.ts.endsWith('Z') ? ev.ts : ev.ts + 'Z').toLocaleTimeString()}</span>
                {' · '}
                <span style={{ color: '#8a96ad' }}>{ev.event}</span>
                {' · '}
                {ev.summary}
              </div>
            ))
          )}
        </div>
      </Card>
    </div>
  )
}
