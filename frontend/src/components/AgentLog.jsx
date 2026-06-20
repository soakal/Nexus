import { useEffect, useRef, useState } from 'react'
import { connectWS } from '../lib/ws'
import StatusDot from './StatusDot'

export default function AgentLog() {
  const [lines, setLines] = useState([])
  const ref = useRef(null)
  useEffect(() => {
    const unsub = connectWS(msg => setLines(l => [...l.slice(-200), msg]))
    return unsub
  }, [])
  useEffect(() => { ref.current?.scrollTo(0, ref.current.scrollHeight) }, [lines])

  return (
    <div
      ref={ref}
      style={{
        background: '#070a11',
        border: '1px solid rgba(120,160,220,0.10)',
        borderRadius: 12,
        padding: 18,
        minHeight: 150,
        maxHeight: 260,
        overflowY: 'auto',
        fontFamily: "'JetBrains Mono', monospace",
        fontSize: 12,
        position: 'relative',
      }}
    >
      {lines.length === 0 ? (
        <span style={{ fontStyle: 'italic', color: '#5d6982' }}>
          Waiting for agent activity…
        </span>
      ) : (
        <>
          {lines.map((l, i) => (
            <div key={i} style={{ color: '#94a6c0', lineHeight: 1.6 }}>{l}</div>
          ))}
        </>
      )}

      {/* LIVE badge */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 10,
        letterSpacing: '0.1em',
        color: '#5fe0b4',
        fontWeight: 700,
        position: 'absolute',
        bottom: 14,
        right: 16,
      }}>
        <StatusDot color="#34d399" size={6} pulse />
        LIVE
      </div>
    </div>
  )
}
