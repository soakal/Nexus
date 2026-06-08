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
    <div ref={ref} className="bg-bg-secondary border border-border-dark rounded-lg p-3 h-64 overflow-y-auto font-mono text-xs text-accent-green">
      {lines.length === 0 ? <span className="text-text-secondary">Waiting for agent activity...</span> : lines.map((l, i) => <div key={i} className="animate-fade-in">{l}</div>)}
    </div>
  )
}
