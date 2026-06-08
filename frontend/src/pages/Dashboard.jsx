import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import WeatherCard from '../components/WeatherCard'
import AdGuardToggle from '../components/AdGuardToggle'
import SourceCard from '../components/SourceCard'
import RecordingCard from '../components/RecordingCard'

export default function Dashboard() {
  const [sources, setSources] = useState({})
  const [weather, setWeather] = useState(null)
  const [adguard, setAdguard] = useState(null)
  const [channels, setChannels] = useState(null)
  const [unraid, setUnraid] = useState(null)
  const [dockerOpen, setDockerOpen] = useState(false)
  const [briefingLoading, setBriefingLoading] = useState(false)
  const [lastBriefing, setLastBriefing] = useState(null)

  useEffect(() => {
    const load = async () => {
      try { setSources(await api.sources.status()) } catch {}
      try { setWeather(await api.get('/weather') || null) } catch {}
      try { setAdguard(await api.adguard.get()) } catch {}
      try { setChannels(await api.channels.get()) } catch {}
      try { setUnraid(await api.unraid.get()) } catch {}
      try { const b = await api.briefing.latest(); setLastBriefing(b?.created_at) } catch {}
    }
    load()
    const timer = setInterval(load, 30000)
    return () => clearInterval(timer)
  }, [])

  const runBriefing = async () => {
    setBriefingLoading(true)
    try { await api.briefing.trigger() } catch {}
    setBriefingLoading(false)
  }

  const restartDocker = async (id) => {
    try { await api.unraid.restartDocker(id) } catch {}
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="font-mono text-accent-cyan text-xl font-bold tracking-wider">NEXUS COMMAND CENTER</h1>
        <div className="text-right">
          <button onClick={runBriefing} disabled={briefingLoading}
            className="bg-accent-cyan text-bg-primary font-mono text-sm px-4 py-2 rounded font-bold hover:opacity-90 disabled:opacity-50">
            {briefingLoading ? 'GENERATING...' : 'RUN BRIEFING'}
          </button>
          {lastBriefing && <div className="text-text-secondary text-xs mt-1">Last: {new Date(lastBriefing.endsWith('Z') ? lastBriefing : lastBriefing + 'Z').toLocaleTimeString()}</div>}
        </div>
      </div>

      {/* Weather strip */}
      {weather ? <WeatherCard data={weather} /> : <div className="bg-bg-card border border-border-dark rounded-lg p-4 text-text-secondary text-sm">Weather loading...</div>}

      {/* Source grid */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        {Object.entries(sources).map(([name, data]) => (
          <SourceCard key={name} name={name} healthy={data.healthy} lastChecked={data.last_checked} />
        ))}
      </div>

      {/* AdGuard card */}
      {adguard && (
        <div className="bg-bg-card border border-border-dark rounded-lg p-4">
          <div className="flex items-center justify-between">
            <span className="text-text-secondary text-xs uppercase tracking-wider">AdGuard Home</span>
            <AdGuardToggle enabled={adguard.filtering_enabled} />
          </div>
          <div className="mt-2 font-mono text-text-primary">
            {adguard.blocked_today} blocked ({adguard.blocked_pct}%)
          </div>
          <div className="text-text-secondary text-xs">{adguard.queries_today} total queries today</div>
        </div>
      )}

      {/* Channels DVR card */}
      {channels && (
        <div className="bg-bg-card border border-border-dark rounded-lg p-4">
          <div className="text-text-secondary text-xs uppercase tracking-wider mb-2">Channels DVR</div>
          {channels.recording_now?.length > 0 ? (
            <div className="space-y-2 mb-2">
              {channels.recording_now.map((r, i) => <RecordingCard key={i} recording={r} />)}
            </div>
          ) : (
            <div className="text-text-secondary text-sm mb-2">Nothing recording</div>
          )}
          <div className="w-full bg-border-dark rounded-full h-2">
            <div className="bg-accent-cyan h-2 rounded-full" style={{ width: `${channels.storage_total_gb ? (channels.storage_used_gb / channels.storage_total_gb * 100) : 0}%` }} />
          </div>
          <div className="text-text-secondary text-xs mt-1">{channels.storage_used_gb}GB / {channels.storage_total_gb}GB</div>
        </div>
      )}

      {/* Unraid card */}
      {unraid && (
        <div className="bg-bg-card border border-border-dark rounded-lg p-4">
          <div className="flex items-center justify-between cursor-pointer" onClick={() => setDockerOpen(o => !o)}>
            <span className="text-text-secondary text-xs uppercase tracking-wider">Unraid</span>
            <span className={`text-xs font-mono ${unraid.array_status === 'started' ? 'text-accent-green' : 'text-accent-orange'}`}>
              {unraid.array_status}
            </span>
          </div>
          <div className="flex gap-4 mt-2 text-xs text-text-secondary">
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
