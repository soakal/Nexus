import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { fmtTime } from '../lib/parseUTC'
import WeatherCard from '../components/WeatherCard'
import AdGuardToggle from '../components/AdGuardToggle'
import SourceCard from '../components/SourceCard'
import RecordingCard from '../components/RecordingCard'
import BrainOrganizerCard from '../components/BrainOrganizerCard'

export default function Dashboard() {
  const [sources, setSources] = useState({})
  const [weather, setWeather] = useState(null)
  const [adguard, setAdguard] = useState(null)
  const [channels, setChannels] = useState(null)
  const [unraid, setUnraid] = useState(null)
  const [brain, setBrain] = useState(null)
  const [dockerOpen, setDockerOpen] = useState(false)
  const [briefingLoading, setBriefingLoading] = useState(false)
  const [briefingError, setBriefingError] = useState(false)
  const [lastBriefing, setLastBriefing] = useState(null)
  const navigate = useNavigate()

  const load = useCallback(() => {
    api.sources.status().then(setSources).catch(() => {})
    api.get('/weather').then(d => setWeather(d || null)).catch(() => {})
    api.adguard.get().then(setAdguard).catch(() => {})
    api.channels.get().then(setChannels).catch(() => {})
    api.unraid.get().then(setUnraid).catch(() => {})
    api.briefing.latest().then(b => setLastBriefing(b?.created_at)).catch(() => {})
    api.brain.status().then(setBrain).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 15000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load])

  const runBriefing = async () => {
    setBriefingLoading(true)
    setBriefingError(false)
    try {
      await api.briefing.trigger()
      try {
        const b = await api.briefing.latest()
        setLastBriefing(b?.created_at)
      } catch {}
      navigate('/briefing')
    } catch (e) {
      setBriefingError(true)
    } finally {
      setBriefingLoading(false)
    }
  }

  const restartDocker = async (id) => {
    try { await api.unraid.restartDocker(id); load() } catch {}
  }

  const lastBriefingTime = fmtTime(lastBriefing)

  return (
    <div className="p-4 md:p-6 space-y-6">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <h1 className="page-header">NEXUS COMMAND CENTER</h1>
        <div className="text-left sm:text-right">
          <button onClick={runBriefing} disabled={briefingLoading}
            className="glow-btn px-4 py-2 disabled:opacity-50">
            {briefingLoading ? 'GENERATING...' : 'RUN BRIEFING'}
          </button>
          {lastBriefingTime && (
            <div className="hud-label mt-1">Last: {lastBriefingTime}</div>
          )}
          {briefingError && (
            <div className="hud-label mt-1 text-accent-orange">BRIEFING FAILED — CHECK CONNECTION</div>
          )}
        </div>
      </div>

      {/* Weather strip */}
      <div>
        <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">ENVIRONMENT</div>
        {weather && weather.temp_f != null
          ? <WeatherCard data={weather} />
          : <div className="hud-panel p-4 text-text-secondary text-sm">Weather loading...</div>
        }
      </div>

      {/* Source grid */}
      <div>
        <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">SYSTEM SOURCES</div>
        <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {Object.entries(sources).map(([name, data]) => (
            <SourceCard key={name} name={name} healthy={data.healthy} lastChecked={data.last_checked} />
          ))}
        </div>
      </div>

      {/* AdGuard card */}
      {adguard && (
        <div className="hud-panel p-4">
          <div className="flex items-center justify-between">
            <span className="hud-label border-l-2 border-accent-cyan pl-2">AdGuard Home</span>
            <AdGuardToggle enabled={adguard.filtering_enabled} />
          </div>
          <div className="mt-2 font-mono text-text-primary glow-cyan-text">
            {adguard.blocked_today} blocked ({adguard.blocked_pct}%)
          </div>
          <div className="text-text-secondary text-xs font-mono">{adguard.queries_today} total queries today</div>
        </div>
      )}

      {/* Channels DVR card */}
      {channels && (
        <div className="hud-panel p-4">
          <div className="hud-label border-l-2 border-accent-cyan pl-2 mb-3">CHANNELS DVR</div>
          {channels.recording_now?.length > 0 ? (
            <div className="space-y-2 mb-3">
              {channels.recording_now.map((r, i) => <RecordingCard key={i} recording={r} />)}
            </div>
          ) : (
            <div className="hud-label mb-3 opacity-50">NOTHING RECORDING</div>
          )}
          {channels.storage_total_gb > 0 ? (
            <>
              <div className="flex items-center justify-between mb-1">
                <span className="hud-label">{channels.storage_used_gb}GB / {channels.storage_total_gb}GB</span>
                <span className="hud-label">{Math.round(channels.storage_used_gb / channels.storage_total_gb * 100)}%</span>
              </div>
              <div className="w-full bg-bg-secondary border border-border-dark h-2">
                <div
                  className="bg-accent-cyan h-full"
                  style={{ width: `${channels.storage_used_gb / channels.storage_total_gb * 100}%`, boxShadow: '0 0 8px rgba(0,212,255,0.6)' }}
                />
              </div>
            </>
          ) : (
            <div className="hud-label opacity-40">STORAGE DATA UNAVAILABLE</div>
          )}
        </div>
      )}

      {/* Brain Organizer card */}
      {brain !== null && (
        <BrainOrganizerCard data={brain} onRun={load} />
      )}

      {/* Unraid card */}
      {unraid && (
        <div className="hud-panel p-4">
          <div className="flex items-center justify-between cursor-pointer" onClick={() => setDockerOpen(o => !o)}>
            <span className="hud-label border-l-2 border-accent-cyan pl-2">Unraid</span>
            <div className="flex items-center gap-1.5">
              <span className={unraid.array_status === 'started' ? 'arc-dot' : 'arc-dot-warn'} />
              <span className={`text-xs font-mono uppercase tracking-wider ${unraid.array_status === 'started' ? 'text-accent-green' : 'text-accent-orange'}`}>
                {unraid.array_status}
              </span>
            </div>
          </div>
          <div className="flex gap-4 mt-2 text-xs text-text-secondary font-mono">
            {unraid.parity_status === 'running' && <span className="text-accent-orange">⚠ Parity running</span>}
            {unraid.mover_running && <span className="text-accent-cyan">● Mover active</span>}
            <span>{unraid.docker_containers?.length || 0} containers</span>
          </div>
          {dockerOpen && (
            <div className="mt-3 space-y-2 border-t border-border-dark pt-3">
              {(unraid.docker_containers || []).map(c => (
                <div key={c.id} className="flex items-center justify-between text-sm">
                  <span className="text-text-primary">{c.name}</span>
                  <div className="flex items-center gap-2">
                    <span className={`text-xs font-mono ${c.status === 'running' ? 'text-accent-green' : 'text-text-secondary'}`}>{c.status}</span>
                    <button onClick={() => restartDocker(c.id)} className="text-accent-cyan text-xs hover:underline">restart</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
