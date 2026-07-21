import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { fmtTime } from '../lib/parseUTC'
import BrainOrganizerCard from '../components/BrainOrganizerCard'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import StatusPill from '../components/StatusPill'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'

export default function Dashboard() {
  const [sources, setSources] = useState({})
  const [weather, setWeather] = useState(null)
  const [adguard, setAdguard] = useState(null)
  const [channels, setChannels] = useState(null)
  const [unraid, setUnraid] = useState(null)
  const [proxmox, setProxmox] = useState(null)
  const [proxmoxVmsOpen, setProxmoxVmsOpen] = useState(false)
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
    api.proxmox.get().then(setProxmox).catch(() => {})
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

  const restartDocker = async (id, name) => {
    if (!window.confirm(`Restart ${name || 'this container'}?`)) return
    try { await api.unraid.restartDocker(id); load() } catch {}
  }

  const [vmActionBusy, setVmActionBusy] = useState(null)
  const runVmAction = async (vm, action) => {
    if (!window.confirm(`${action[0].toUpperCase()}${action.slice(1)} ${vm}?`)) return
    setVmActionBusy(vm)
    try {
      await api.safety.executeHermesAction('vm_action', { vm, action })
      load()
    } catch {
    } finally {
      setVmActionBusy(null)
    }
  }

  const lastBriefingTime = fmtTime(lastBriefing)

  // Source counts
  const srcVals = Object.values(sources || {})
  const online = srcVals.filter(s => s.healthy).length
  const total = srcVals.length

  // DVR storage pct
  const pct = channels && channels.storage_total_gb > 0
    ? Math.round(channels.storage_used_gb / channels.storage_total_gb * 100)
    : 0

  // Current time for "Synced" label
  const syncedTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const nowStr = new Date().toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

  return (
    <div style={{ width: '100%', maxWidth: '1480px', margin: '0 auto', padding: 'clamp(16px,3vw,32px)', display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>

      {/* Header */}
      <ScreenHeader
        section="Dashboard"
        title="Command Center"
        subline={nowStr}
        right={
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '9px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <StatusPill
                tone="green"
                label={`${online} / ${total || 10} online`}
              />
              <PrimaryButton
                onClick={runBriefing}
                disabled={briefingLoading}
                icon={
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M5 3l14 9-14 9z"/>
                  </svg>
                }
              >
                {briefingLoading ? 'Generating…' : 'Run briefing'}
              </PrimaryButton>
            </div>
            <div style={{ fontSize: '11px', color: '#5d6982' }}>
              Last briefing <span style={{ color: '#8a96ad' }}>{lastBriefingTime || '—'}</span>
            </div>
            {briefingError && (
              <div style={{ fontSize: '11px', color: '#fb7185' }}>Briefing failed — check connection</div>
            )}
          </div>
        }
      />

      {/* KPI Row */}
      <section style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--gap)' }}>

        {/* Weather */}
        {weather && (
          <Card style={{ flex: '2.2 1 300px', position: 'relative', overflow: 'hidden' }}>
            <div style={{ position: 'absolute', top: '-30px', right: '-20px', width: '160px', height: '160px', borderRadius: '50%', background: 'radial-gradient(circle,rgba(251,191,36,0.16),transparent 70%)' }} />
            <Eyebrow>Environment</Eyebrow>
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginTop: '16px' }}>
              <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="1.6">
                <circle cx="12" cy="12" r="4.2"/>
                <path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>
              </svg>
              <div>
                <div style={{ fontSize: '38px', fontWeight: 700, lineHeight: 1, letterSpacing: '-0.02em' }}>
                  {weather.temp_f}°
                </div>
                <div style={{ fontSize: '13px', color: '#8a96ad', marginTop: '5px' }}>
                  {weather.summary || weather.condition}
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '26px', marginTop: '18px', paddingTop: '16px', borderTop: '1px solid rgba(120,160,220,0.10)' }}>
              <div>
                <div style={{ fontSize: '11px', color: '#5d6982', letterSpacing: '0.06em' }}>HIGH / LOW</div>
                <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '3px' }}>
                  {weather.high_f}° / {weather.low_f}°
                </div>
              </div>
              <div>
                <div style={{ fontSize: '11px', color: '#5d6982', letterSpacing: '0.06em' }}>WIND</div>
                <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '3px' }}>
                  {weather.wind_mph} mph
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* Sources */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>Sources</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700 }}>
              {online}<span style={{ fontSize: '17px', color: '#5d6982', fontWeight: 500 }}>/{total || 10}</span>
            </div>
            <div style={{ fontSize: '12px', color: '#5fe0b4', marginTop: '4px', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <StatusDot color="#34d399" size={6} glow={false} />
              All online
            </div>
          </div>
        </Card>

        {/* Blocked */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>Blocked</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700 }}>
              {adguard?.blocked_pct || 0}<span style={{ fontSize: '17px', color: '#5d6982', fontWeight: 500 }}>%</span>
            </div>
            <div style={{ fontSize: '12px', color: '#8a96ad', marginTop: '4px' }}>
              {adguard?.blocked_today} today
            </div>
            <div style={{ height: '4px', borderRadius: '3px', background: 'rgba(120,160,220,0.12)', marginTop: '8px', overflow: 'hidden' }}>
              <div style={{ width: `${adguard?.blocked_pct || 0}%`, height: '100%', background: 'var(--accent)', borderRadius: '3px' }} />
            </div>
          </div>
        </Card>

        {/* DVR Storage */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>DVR Storage</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700 }}>
              {pct}<span style={{ fontSize: '17px', color: '#5d6982', fontWeight: 500 }}>%</span>
            </div>
            <div style={{ fontSize: '12px', color: '#8a96ad', marginTop: '4px' }}>
              {(channels?.storage_used_gb / 1000 || 0).toFixed(2)} / {(channels?.storage_total_gb / 1000 || 0).toFixed(2)} TB
            </div>
            <div style={{ height: '4px', borderRadius: '3px', background: 'rgba(120,160,220,0.12)', marginTop: '8px', overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: '#5b8cff', borderRadius: '3px' }} />
            </div>
          </div>
        </Card>

        {/* Brain Queue */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>Brain Queue</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700, color: '#fbbf24' }}>
              {brain?.pending || 0}
            </div>
            <div style={{ fontSize: '12px', color: '#8a96ad', marginTop: '4px' }}>items pending</div>
          </div>
        </Card>
      </section>

      {/* System Sources */}
      <Card>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: '10px', marginBottom: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px' }}>
            <Eyebrow>System Sources</Eyebrow>
            <span style={{ fontSize: '12px', color: '#5fe0b4', fontWeight: 500 }}>{online} connected</span>
          </div>
          <span style={{ fontSize: '11px', color: '#5d6982' }}>Synced {syncedTime}</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px' }}>
          {Object.entries(sources || {}).map(([name, data]) => (
            <div key={name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', padding: '13px 14px', borderRadius: '11px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)' }}>
              <span style={{ fontSize: '13px', fontWeight: 600, color: '#dbe3f0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: 'none' }}>
                <StatusDot color={data.healthy ? '#34d399' : '#fb7185'} size={7} glow={false} />
                <span style={{ fontSize: '10px', letterSpacing: '0.08em', fontWeight: 600, color: data.healthy ? '#5fe0b4' : '#fb7185' }}>
                  {data.healthy ? 'ONLINE' : 'OFFLINE'}
                </span>
              </span>
            </div>
          ))}
        </div>
      </Card>

      {/* AdGuard + Channels DVR row */}
      <section style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--gap)' }}>

        {/* AdGuard */}
        <Card style={{ flex: '1.6 1 420px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', flexWrap: 'wrap' }}>
            <Eyebrow>AdGuard Home</Eyebrow>
            <StatusPill
              tone={adguard?.filtering_enabled ? 'green' : 'grey'}
              label={adguard?.filtering_enabled ? 'Filtering on' : 'Off'}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: '10px', marginTop: '18px' }}>
            <span style={{ fontSize: '40px', fontWeight: 700, lineHeight: 1, letterSpacing: '-0.02em' }}>
              {adguard?.blocked_today}
            </span>
            <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--accent)', paddingBottom: '5px' }}>
              {adguard?.blocked_pct}%
            </span>
          </div>
          <div style={{ fontSize: '13px', color: '#8a96ad', marginTop: '7px' }}>
            queries blocked of {adguard?.queries_today} total today
          </div>
          <div style={{ height: '8px', borderRadius: '5px', background: 'rgba(120,160,220,0.12)', marginTop: '18px', overflow: 'hidden' }}>
            <div style={{ width: `${adguard?.blocked_pct || 0}%`, height: '100%', background: 'linear-gradient(90deg,var(--accent),#2477c9)' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '14px', flexWrap: 'wrap', marginTop: '18px', paddingTop: '16px', borderTop: '1px solid rgba(120,160,220,0.10)' }}>
            <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap' }}>
              <div>
                <div style={{ fontSize: '11px', color: '#5d6982' }}>QUERIES</div>
                <div style={{ fontSize: '16px', fontWeight: 600, marginTop: '3px' }}>{adguard?.queries_today}</div>
              </div>
              <div>
                <div style={{ fontSize: '11px', color: '#5d6982' }}>BLOCKED</div>
                <div style={{ fontSize: '16px', fontWeight: 600, marginTop: '3px' }}>{adguard?.blocked_today}</div>
              </div>
              <div>
                <div style={{ fontSize: '11px', color: '#5d6982' }}>ALLOWED</div>
                <div style={{ fontSize: '16px', fontWeight: 600, marginTop: '3px' }}>
                  {(adguard?.queries_today || 0) - (adguard?.blocked_today || 0)}
                </div>
              </div>
            </div>
          </div>
        </Card>

        {/* Channels DVR */}
        <Card style={{ flex: '1 1 280px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <Eyebrow>Channels DVR</Eyebrow>
            <StatusPill
              tone={channels?.recording_now?.length ? 'accent' : 'grey'}
              label={channels?.recording_now?.length ? 'Recording' : 'Idle'}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '20px', marginTop: '18px', flexWrap: 'wrap' }}>
            <svg width="92" height="92" viewBox="0 0 92 92">
              <circle cx="46" cy="46" r="38" fill="none" stroke="rgba(120,160,220,0.14)" strokeWidth="9"/>
              <circle cx="46" cy="46" r="38" fill="none" stroke="#5b8cff" strokeWidth="9" strokeLinecap="round"
                strokeDasharray="238.76" strokeDashoffset={238.76 * (1 - pct / 100)} transform="rotate(-90 46 46)"/>
              <text x="46" y="50" textAnchor="middle" fill="#e9eef8" fontSize="20" fontWeight="700" fontFamily="Space Grotesk">{pct}%</text>
            </svg>
            <div>
              <div style={{ fontSize: '13px', color: '#8a96ad' }}>
                {channels?.recording_now?.length
                  ? `${channels.recording_now.length} recording${channels.recording_now.length !== 1 ? 's' : ''}`
                  : 'No active recordings'}
              </div>
              <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '10px' }}>
                {(channels?.storage_used_gb / 1000 || 0).toFixed(2)} TB <span style={{ color: '#5d6982', fontWeight: 500 }}>used</span>
              </div>
              <div style={{ fontSize: '12px', color: '#5d6982', marginTop: '3px' }}>
                of {(channels?.storage_total_gb / 1000 || 0).toFixed(2)} TB capacity
              </div>
            </div>
          </div>
        </Card>
      </section>

      {/* Brain + Unraid row */}
      <section style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--gap)' }}>

        <BrainOrganizerCard data={brain} onRun={load} style={{ flex: '1.8 1 460px' }} />

        {/* Unraid */}
        {unraid && (
          <Card style={{ flex: '1 1 240px', display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <Eyebrow>Unraid</Eyebrow>
              <StatusPill
                tone={unraid.array_status === 'started' ? 'green' : 'amber'}
                dotRing={unraid.array_status === 'started'}
                label={unraid.array_status === 'started' ? 'Started' : (unraid.array_status || 'Unknown')}
              />
            </div>
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', padding: '18px 0' }}>
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5">
                <rect x="3" y="4" width="18" height="6" rx="1.5"/>
                <rect x="3" y="14" width="18" height="6" rx="1.5"/>
                <path d="M7 7h.01M7 17h.01"/>
              </svg>
              <div style={{ fontSize: '40px', fontWeight: 700, marginTop: '10px' }}>
                {unraid.docker_containers?.length || 0}
              </div>
              <div style={{ fontSize: '13px', color: '#8a96ad' }}>containers running</div>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', maxHeight: dockerOpen ? '160px' : 'none', overflowY: dockerOpen ? 'auto' : 'visible' }}>
              {(unraid.docker_containers || []).slice(0, dockerOpen ? undefined : 2).map(c => (
                <div
                  key={c.id}
                  onClick={() => restartDocker(c.id, c.name)}
                  style={{ flex: '1 1 45%', display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)', cursor: 'pointer' }}
                >
                  <StatusDot color="#34d399" size={7} glow={false} />
                  <span style={{ fontSize: '12px', color: '#cdd6e6', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {c.name || 'Up'}
                  </span>
                </div>
              ))}
              {/* Pad to 2 chips if fewer containers */}
              {(unraid.docker_containers?.length || 0) === 0 && (
                <>
                  <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)' }}>
                    <StatusDot color="#34d399" size={7} glow={false} />
                    <span style={{ fontSize: '12px', color: '#cdd6e6', fontWeight: 500 }}>Up</span>
                  </div>
                  <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)' }}>
                    <StatusDot color="#34d399" size={7} glow={false} />
                    <span style={{ fontSize: '12px', color: '#cdd6e6', fontWeight: 500 }}>Up</span>
                  </div>
                </>
              )}
            </div>
            {(unraid.docker_containers?.length || 0) > 2 && (
              <button
                onClick={() => setDockerOpen(v => !v)}
                style={{ fontSize: '11px', fontWeight: 600, color: '#5d6982', background: 'none', border: 'none', cursor: 'pointer', padding: '8px 0 0', textAlign: 'left' }}
              >
                {dockerOpen ? 'Show less' : `+${unraid.docker_containers.length - 2} more`}
              </button>
            )}
          </Card>
        )}

        {/* Proxmox */}
        {proxmox && (
          <Card style={{ flex: '1 1 240px', display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <Eyebrow>Proxmox</Eyebrow>
              <StatusPill
                tone={proxmox.node_status === 'online' ? 'green' : 'amber'}
                dotRing={proxmox.node_status === 'online'}
                label={proxmox.node_status === 'online' ? 'Online' : (proxmox.node_status || 'Unknown')}
              />
            </div>
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', padding: '18px 0' }}>
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5">
                <rect x="3" y="3" width="7" height="7" rx="1.5"/>
                <rect x="14" y="3" width="7" height="7" rx="1.5"/>
                <rect x="3" y="14" width="7" height="7" rx="1.5"/>
                <rect x="14" y="14" width="7" height="7" rx="1.5"/>
              </svg>
              <div style={{ fontSize: '40px', fontWeight: 700, marginTop: '10px' }}>
                {proxmox.vms?.length || 0}
              </div>
              <div style={{ fontSize: '13px', color: '#8a96ad' }}>VMs / containers</div>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', maxHeight: proxmoxVmsOpen ? '160px' : 'none', overflowY: proxmoxVmsOpen ? 'auto' : 'visible' }}>
              {(proxmox.vms || []).slice(0, proxmoxVmsOpen ? undefined : 4).map(v => (
                <div
                  key={v.vmid}
                  style={{ flex: '1 1 45%', display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)' }}
                >
                  <StatusDot color={v.status === 'running' ? '#34d399' : '#8a96ad'} size={7} glow={false} />
                  <span style={{ fontSize: '12px', color: '#cdd6e6', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                    {v.name || v.vmid}
                  </span>
                  <select
                    value=""
                    disabled={vmActionBusy === v.name}
                    onChange={(e) => { const action = e.target.value; e.target.value = ''; if (action) runVmAction(v.name, action) }}
                    style={{ fontSize: '11px', background: 'rgba(255,255,255,0.04)', color: '#8a96ad', border: '1px solid rgba(120,160,220,0.12)', borderRadius: '6px', padding: '2px 4px' }}
                  >
                    <option value="">&hellip;</option>
                    {v.status === 'running' ? (
                      <>
                        <option value="reboot">Reboot</option>
                        <option value="stop">Stop</option>
                      </>
                    ) : (
                      <option value="start">Start</option>
                    )}
                  </select>
                </div>
              ))}
            </div>
            {(proxmox.vms?.length || 0) > 4 && (
              <button
                onClick={() => setProxmoxVmsOpen(v => !v)}
                style={{ fontSize: '11px', fontWeight: 600, color: '#5d6982', background: 'none', border: 'none', cursor: 'pointer', padding: '8px 0 0', textAlign: 'left' }}
              >
                {proxmoxVmsOpen ? 'Show less' : `+${proxmox.vms.length - 4} more`}
              </button>
            )}
          </Card>
        )}
      </section>
    </div>
  )
}
