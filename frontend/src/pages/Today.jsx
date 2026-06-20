import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import ScreenHeader from '../components/ScreenHeader'

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

  const load = useCallback(() => {
    api.today.get().then(setData).catch(() => {})
  }, [])

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
