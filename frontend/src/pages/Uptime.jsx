import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import TrendChart from '../components/TrendChart'

export default function Uptime() {
  const [summary, setSummary] = useState(null)
  const [speedtest, setSpeedtest] = useState(null)
  const [liveStatus, setLiveStatus] = useState({})

  const load = useCallback(() => {
    api.uptime.summary(7).then(setSummary).catch(() => {})
    api.uptime.speedtest(7).then(setSpeedtest).catch(() => {})
    // Live current status comes from the same cached health checks the dashboard
    // uses, so the dot recovers in seconds instead of lagging the 2-min sampler.
    api.sources.status().then(setLiveStatus).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 30000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load])

  const sources = summary?.sources ?? null
  const speedData = speedtest?.data ?? null
  const latest = speedtest?.latest ?? null

  // Map speedtest history for TrendChart
  const downloadChartData = speedData
    ? speedData.map(d => ({ timestamp: d.timestamp, value: d.download_mbps }))
    : []

  return (
    <div className="p-4 md:p-6 max-w-4xl">
      <h1 className="page-header mb-6">UPTIME &amp; CONNECTIVITY</h1>

      {/* Source uptime grid */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">SOURCE UPTIME — LAST 7 DAYS</div>

      {sources === null ? (
        <div className="hud-label animate-pulse mb-6">LOADING...</div>
      ) : sources.length === 0 ? (
        <div className="hud-label opacity-40 mb-6">NO DATA YET — COLLECTING...</div>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 mb-8">
          {sources.map((s) => {
            // Prefer live cached status for the dot; fall back to the last sample.
            const live = liveStatus[s.source]
            const isUp = live ? live.healthy : s.current_ok
            return (
            <div
              key={s.source}
              className="hud-panel-sm p-3"
              style={{ borderColor: isUp ? 'rgba(0,212,255,0.2)' : 'rgba(255,45,45,0.3)' }}
            >
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-xs text-text-primary uppercase tracking-wider">
                  {s.source}
                </span>
                <div className="flex items-center gap-1.5">
                  <span className={isUp ? 'arc-dot' : 'arc-dot-err'} />
                  <span className="hud-label">{isUp ? 'UP' : 'DOWN'}</span>
                </div>
              </div>
              <div className={`font-mono text-lg glow-cyan-text ${isUp ? 'text-text-primary' : 'text-accent-orange'}`}>
                {s.uptime_pct}%
              </div>
              <div className="text-text-secondary text-xs font-mono mt-1">
                {s.avg_latency_ms}ms avg &middot; {s.samples} samples
              </div>
            </div>
            )
          })}
        </div>
      )}

      {/* Internet Speed section */}
      <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">INTERNET SPEED</div>

      {speedData === null ? (
        <div className="hud-label animate-pulse">LOADING...</div>
      ) : speedData.length === 0 ? (
        <div className="hud-label opacity-40">NO DATA YET — COLLECTING...</div>
      ) : (
        <>
          {/* Latest stat blocks */}
          {latest && (
            <div className="grid grid-cols-3 gap-3 mb-4">
              <div className="hud-panel-sm p-3">
                <div className="hud-label mb-1">DOWNLOAD</div>
                <div className="font-mono text-lg text-text-primary glow-cyan-text">
                  {latest.download_mbps} <span className="text-xs text-text-secondary">Mbps</span>
                </div>
              </div>
              <div className="hud-panel-sm p-3">
                <div className="hud-label mb-1">UPLOAD</div>
                <div className="font-mono text-lg text-text-primary glow-cyan-text">
                  {latest.upload_mbps} <span className="text-xs text-text-secondary">Mbps</span>
                </div>
              </div>
              <div className="hud-panel-sm p-3">
                <div className="hud-label mb-1">PING</div>
                <div className="font-mono text-lg text-text-primary glow-cyan-text">
                  {latest.ping_ms} <span className="text-xs text-text-secondary">ms</span>
                </div>
              </div>
            </div>
          )}

          {/* Download history chart */}
          <TrendChart
            title="Download Speed (Mbps)"
            data={downloadChartData}
            unit=" Mbps"
          />
        </>
      )}
    </div>
  )
}
