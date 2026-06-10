import { useEffect, useRef, useState } from 'react'
import { connectWS } from '../lib/ws'
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
      className="hud-panel p-3 h-64 overflow-y-auto font-mono text-xs relative"
      style={{ backgroundColor: '#04080f', color: '#00ff9d' }}
    >
      {lines.length === 0 ? (
        <span className="text-text-secondary italic">Waiting for agent activity...</span>
      ) : (
        <>
          {lines.map((l, i) => (
            <div key={i} style={{ animation: 'data-flicker 3s ease-in-out infinite' }}>{l}</div>
          ))}
          <span style={{ animation: 'pulse-glow 1s steps(1) infinite' }}>|</span>
        </>
      )}
      <div className="absolute bottom-2 right-2 flex items-center gap-1.5">
        <span className="arc-dot" />
        <span className="hud-label">LIVE</span>
      </div>
    </div>
  )
}
