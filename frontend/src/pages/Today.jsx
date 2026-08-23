import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import ScreenHeader from '../components/ScreenHeader'
import { parsePriorityActions } from '../lib/priorityActions'

const DONE_PREFIX = 'nexus_today_done:'

// localStorage helpers — all storage access is wrapped so disabled/full storage
// degrades to in-memory only (no crash).
function loadDone(briefingId) {
  try {
    // Sweep away stale keys from previous briefings so localStorage doesn't grow
    // unbounded — only the current briefing's checked-state survives.
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i)
      if (k && k.startsWith(DONE_PREFIX) && k !== `${DONE_PREFIX}${briefingId}`) {
        localStorage.removeItem(k)
      }
    }
    const raw = localStorage.getItem(`${DONE_PREFIX}${briefingId}`)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function saveDone(briefingId, doneArr) {
  try {
    localStorage.setItem(`${DONE_PREFIX}${briefingId}`, JSON.stringify(doneArr))
  } catch {
    // storage disabled/full — in-memory state (React) still works this session.
  }
}

// Render a calendar line with time highlighted in accent cyan if it matches HH:MM AM/PM pattern
function AgendaLine({ line }) {
  const match = line.match(/^(\s*)(\d{1,2}:\d{2}\s*[AP]M)\s+(.*)/)
  if (match) {
    return (
      <div style={{ lineHeight: 1.7 }}>
        <span style={{ color: 'var(--accent)', fontWeight: 600, fontFamily: "'IBM Plex Mono', monospace", fontSize: '13px' }}>{match[2]}</span>
        {' '}
        <span style={{ color: '#ece9e2', fontSize: '14px' }}>{match[3]}</span>
      </div>
    )
  }
  return <div style={{ color: '#ece9e2', fontSize: '14px', lineHeight: 1.7 }}>{line}</div>
}

export default function Today() {
  const [data, setData] = useState(null)
  const [briefing, setBriefing] = useState(null)
  const [done, setDone] = useState([])
  const [homeState, setHomeState] = useState(null)

  const load = useCallback(() => {
    // calendar/email used to be a live api.today.get() call on every 120s
    // poll -- both underlying caches are also 120s TTL, so almost every poll
    // missed and paid a live iCal fetch + Proton MCP round trip. Reads the
    // cached dashboard.today collector instead (600s TTL, plenty fresh for
    // an agenda/inbox-summary card). homeState stays live -- HA's own 30s
    // cache already keeps it cheap, and stale lock/door state would be
    // actively misleading, not just slower.
    api.dashboard.state().then(d => { if (d?.today?.data) setData(d.today.data) }).catch(() => {})
    api.today.homeState().then(setHomeState).catch(() => {})
    api.briefing.latest()
      .then((b) => {
        setBriefing(b)
        if (b && b.id != null) setDone(loadDone(b.id))
      })
      .catch(() => setBriefing(null))  // 404s when no briefing exists -> hide card
  }, [])

  const toggle = useCallback((idx) => {
    setDone((prev) => {
      const next = prev.includes(idx)
        ? prev.filter((i) => i !== idx)
        : [...prev, idx]
      if (briefing && briefing.id != null) saveDone(briefing.id, next)
      return next
    })
  }, [briefing])

  useEffect(() => {
    load()
    const timer = setInterval(load, 120000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load])

  const calendarLines = data?.calendar
    ? data.calendar.split('\n')
    : []

  const priority = briefing?.content
    ? parsePriorityActions(briefing.content)
    : { items: [], note: '' }
  const showPriority = priority.items.length > 0 || !!priority.note

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
      <ScreenHeader section="Today" title="Today" />

      {!data ? (
        <div style={{ color: '#7a776d', fontSize: '13px' }}>Loading…</div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--gap)' }}>

          {/* Priority Actions card — rendered above Agenda when the latest
              briefing has a Priority Actions section. Checkbox state persists
              per-briefing in localStorage. */}
          {showPriority && (
            <Card flex="1 1 100%">
              <Eyebrow style={{ display: 'block', marginBottom: '16px' }}>Priority Actions</Eyebrow>
              {priority.items.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  {priority.items.map((item, i) => {
                    const checked = done.includes(i)
                    return (
                      <label
                        key={i}
                        style={{
                          display: 'flex',
                          alignItems: 'flex-start',
                          gap: '10px',
                          cursor: 'pointer',
                          lineHeight: 1.6,
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggle(i)}
                          style={{ marginTop: '3px', accentColor: 'var(--accent)', cursor: 'pointer' }}
                        />
                        <span
                          style={{
                            color: checked ? '#7a776d' : '#ece9e2',
                            fontSize: '14px',
                            textDecoration: checked ? 'line-through' : 'none',
                          }}
                        >
                          {item}
                        </span>
                      </label>
                    )
                  })}
                </div>
              ) : (
                <div style={{ whiteSpace: 'pre-line', color: '#b3b0a6', fontSize: '13px', lineHeight: 1.7 }}>
                  {priority.note}
                </div>
              )}
            </Card>
          )}

          {/* Home State card — passive glance at notable locks/doors + alerts,
              same data chat's live snapshot already computes (extract_home_state),
              just surfaced here without having to ask. */}
          {homeState?.available && (homeState.locks.length > 0 || homeState.doors.length > 0 || homeState.alert_count > 0) && (
            <Card flex="1 1 280px">
              <Eyebrow style={{ display: 'block', marginBottom: '16px' }}>Home State</Eyebrow>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px', color: '#ece9e2' }}>
                {homeState.alert_count > 0 && (
                  <div style={{ color: '#f0d896' }}>{homeState.alert_count} HA alert{homeState.alert_count === 1 ? '' : 's'}</div>
                )}
                {homeState.locks.map((l) => <div key={l}>{l}</div>)}
                {homeState.doors.map((d) => <div key={d}>{d}</div>)}
              </div>
            </Card>
          )}

          {/* Agenda card */}
          <Card flex="1 1 320px">
            <Eyebrow style={{ display: 'block', marginBottom: '16px' }}>Agenda</Eyebrow>
            {calendarLines.length > 0 ? (
              <div>
                {calendarLines.map((line, i) => (
                  <AgendaLine key={i} line={line} />
                ))}
              </div>
            ) : (
              <div style={{ whiteSpace: 'pre-line', color: '#ece9e2', fontSize: '14px', lineHeight: 1.7 }}>
                {data?.calendar}
              </div>
            )}
          </Card>

          {/* Inbox card */}
          <Card flex="1.4 1 360px">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
              <Eyebrow>Inbox</Eyebrow>
            </div>
            <div style={{ whiteSpace: 'pre-line', color: '#b3b0a6', fontSize: '13px', lineHeight: 1.7 }}>
              {data?.email}
            </div>
          </Card>

        </div>
      )}
    </div>
  )
}
