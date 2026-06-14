import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'

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

  return (
    <div className="p-4 md:p-6 max-w-2xl">
      <h1 className="page-header mb-6">TODAY</h1>

      {!data ? (
        <div className="hud-label animate-pulse">LOADING...</div>
      ) : (
        <div className="space-y-6">
          <div className="hud-panel p-4">
            <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">AGENDA</div>
            <p className="text-text-primary text-sm font-mono whitespace-pre-line">
              {data.calendar}
            </p>
          </div>

          <div className="hud-panel p-4">
            <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">INBOX</div>
            <p className="text-text-primary text-sm font-mono whitespace-pre-line">
              {data.email}
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
