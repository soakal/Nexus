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
        <span style={{ color: 'var(--accent)', fontWeight: 600, fontFamily: "'JetBrains Mono', monospace", fontSize: '13px' }}>{match[2]}</span>
        {' '}
        <span style={{ color: '#dbe3f0', fontSize: '14px' }}>{match[3]}</span>
      </div>
    )
  }
  return <div style={{ color: '#dbe3f0', fontSize: '14px', lineHeight: 1.7 }}>{line}</div>
}

export default function Today() {
  const [data, setData] = useState(null)
  const [briefing, setBriefing] = useState(null)
  const [done, setDone] = useState([])
  const [homeState, setHomeState] = useState(null)

  const load = useCallback(() => {
    api.today.get().then(setData).catch(() => {})
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
        <div style={{ color: '#5d6982', fontSize: '13px' }}>Loading…</div>
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
                            color: checked ? '#5d6982' : '#dbe3f0',
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
                <div style={{ whiteSpace: 'pre-line', color: '#aab4c7', fontSize: '13px', lineHeight: 1.7 }}>
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
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px', color: '#dbe3f0' }}>
                {homeState.alert_count > 0 && (
                  <div style={{ color: '#f4d27a' }}>{homeState.alert_count} HA alert{homeState.alert_count === 1 ? '' : 's'}</div>
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
              <div style={{ whiteSpace: 'pre-line', color: '#dbe3f0', fontSize: '14px', lineHeight: 1.7 }}>
                {data?.calendar}
              </div>
            )}
          </Card>

          {/* Inbox card */}
          <Card flex="1.4 1 360px">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
              <Eyebrow>Inbox</Eyebrow>
            </div>
            <div style={{ whiteSpace: 'pre-line', color: '#aab4c7', fontSize: '13px', lineHeight: 1.7 }}>
              {data?.email}
            </div>
          </Card>

        </div>
      )}
    </div>
  )
}
