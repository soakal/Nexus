import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, wsStateUrl, wsStateProtocols } from '../lib/api'
import { fmtTime } from '../lib/parseUTC'
import BrainOrganizerCard from '../components/BrainOrganizerCard'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import StatusPill from '../components/StatusPill'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'

// claude_usage's captured_at/resets_at are unix EPOCH SECONDS (the statusline
// capture file's native shape), not the ISO strings parseUTC.js's helpers
// expect -- these are deliberately separate, small, local helpers rather than
// stretching parseUTC.js to cover two timestamp shapes.
//
// Returns the FULL "resets in..." clause (not a bare duration) so the call
// site never prefixes its own "resets in " -- doing that used to produce
// "resets in due now" once a capture's window had already elapsed, which is
// the normal overnight state this card exists to show. Includes a day tier
// (7-day window can be 100+ hours out) -- mirrors briefing.py's
// _format_epoch_countdown, same bug class, same fix.
function fmtCountdown(epochSeconds) {
  if (epochSeconds == null) return 'reset time unknown'
  const seconds = epochSeconds - Date.now() / 1000
  if (seconds <= 0) return 'resets any moment'
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  const days = Math.floor(hours / 24)
  if (days > 0) return `resets in ${days}d ${hours % 24}h`
  if (hours > 0) return `resets in ${hours}h ${minutes % 60}m`
  return `resets in ${minutes}m`
}

function fmtAgo(epochSeconds) {
  if (epochSeconds == null) return '—'
  const seconds = Date.now() / 1000 - epochSeconds
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

// Warning-colored, not freshness-colored: this bar communicates how close to
// the rate limit Brian is, same thing the statusline itself conveys -- a
// stale-but-45%-used capture should still read as calm green, not amber,
// since amber here would mean "usage is high," not "data is old."
function usageBarColor(pct) {
  if (pct >= 90) return '#fb7185'
  if (pct >= 70) return '#e8c468'
  return '#5fe0b4'
}

export default function Dashboard() {
  const [sources, setSources] = useState({})
  const [weather, setWeather] = useState(null)
  const [adguard, setAdguard] = useState(null)
  const [channels, setChannels] = useState(null)
  const [unraid, setUnraid] = useState(null)
  const [proxmox, setProxmox] = useState(null)
  const [proxmoxVmsOpen, setProxmoxVmsOpen] = useState(false)
  const [proxmoxMaint, setProxmoxMaint] = useState(null)
  const [brain, setBrain] = useState(null)
  const [claudeUsage, setClaudeUsage] = useState(null)
  const [openrouter, setOpenrouter] = useState(null)
  const [mail, setMail] = useState(null)
  const [mailError, setMailError] = useState(false)
  const [mailOpen, setMailOpen] = useState(false)
  const [dockerOpen, setDockerOpen] = useState(false)
  const [briefingLoading, setBriefingLoading] = useState(false)
  const [briefingError, setBriefingError] = useState(false)
  const [lastBriefing, setLastBriefing] = useState(null)
  const [lastSyncedAt, setLastSyncedAt] = useState(null)
  const [stateFreshness, setStateFreshness] = useState({})
  const navigate = useNavigate()

  const load = useCallback(() => {
    api.dashboard.state().then(snapshot => {
      setSources(snapshot?.sources || {})
      setWeather(snapshot?.weather?.data || null)
      setAdguard(snapshot?.adguard?.data || null)
      setChannels(snapshot?.channels?.data || null)
      setUnraid(snapshot?.unraid?.data || null)
      setProxmox(snapshot?.proxmox?.data || null)
      setProxmoxMaint(snapshot?.proxmox_maintenance?.data || null)
      setBrain(snapshot?.brain?.data || null)
      setClaudeUsage(snapshot?.claude_usage?.data || null)
      setOpenrouter(snapshot?.openrouter?.data || null)
      setMail(snapshot?.mail?.data || null)
      // 'never_observed' (cold-start window before this key's collector group
      // has run once) must degrade the same as 'unavailable' -- otherwise the
      // card just vanishes instead of showing an Offline state, exactly the
      // kind of cold-start gap this feature exists to close.
      setMailError(['unavailable', 'never_observed'].includes(snapshot?.mail?.freshness))
      setLastBriefing(snapshot?.briefing?.data?.created_at || null)
      setLastSyncedAt(snapshot?.generated_at || null)
      setStateFreshness({
        weather: snapshot?.weather?.freshness,
        adguard: snapshot?.adguard?.freshness,
        channels: snapshot?.channels?.freshness,
        unraid: snapshot?.unraid?.freshness,
        proxmox: snapshot?.proxmox?.freshness,
        proxmox_maintenance: snapshot?.proxmox_maintenance?.freshness,
        brain: snapshot?.brain?.freshness,
        mail: snapshot?.mail?.freshness,
        briefing: snapshot?.briefing?.freshness,
        claude_usage: snapshot?.claude_usage?.freshness,
        openrouter: snapshot?.openrouter?.freshness,
      })
    }).catch(() => {})
  }, [])

  useEffect(() => {
    let stopped = false
    let socket = null
    let reconnectTimer = null
    let refreshTimer = null

    const scheduleLoad = () => {
      clearTimeout(refreshTimer)
      refreshTimer = setTimeout(load, 250)
    }

    const connect = () => {
      if (stopped) return
      try {
        socket = new WebSocket(wsStateUrl(), wsStateProtocols())
        socket.onmessage = event => {
          try {
            const message = JSON.parse(event.data)
            if (message.type === 'state.updated') scheduleLoad()
          } catch {}
        }
        socket.onclose = () => {
          if (!stopped) reconnectTimer = setTimeout(connect, 2000)
        }
        socket.onerror = () => socket?.close()
      } catch {
        reconnectTimer = setTimeout(connect, 2000)
      }
    }

    load()
    connect()
    // Slow safety poll covers sleep/resume and networks that block WebSockets.
    const timer = setInterval(load, 60000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      stopped = true
      clearInterval(timer)
      clearTimeout(reconnectTimer)
      clearTimeout(refreshTimer)
      socket?.close()
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

  const restartDocker = async (name) => {
    // Pass the container NAME, not its id -- real Unraid container ids are
    // 129-char PrefixedIDs; the backend resolves name -> id server-side
    // (unraid.resolve_container_id), same as the Telegram restart path.
    if (!window.confirm(`Restart ${name || 'this container'}?`)) return
    try { await api.unraid.restartDocker(name); load() } catch {}
  }

  const [vmActionBusy, setVmActionBusy] = useState(null)
  const runVmAction = async (vmid, name, action) => {
    if (!window.confirm(`${action[0].toUpperCase()}${action.slice(1)} ${name || vmid}?`)) return
    setVmActionBusy(vmid)
    try {
      await api.proxmox.vmPower(vmid, action)
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
  const staleCount = [
    ...srcVals.map(s => s.freshness),
    ...Object.values(stateFreshness),
  ].filter(v => v && v !== 'fresh').length

  // DVR storage pct
  const pct = channels && channels.storage_total_gb > 0
    ? Math.round(channels.storage_used_gb / channels.storage_total_gb * 100)
    : 0

  // "Synced" label reflects the actual snapshot timestamp from the server,
  // not the browser's own clock (which was always "now" regardless of when
  // data last actually refreshed).
  const syncedTime = fmtTime(lastSyncedAt)
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
                tone={online === (total || 10) ? 'green' : 'amber'}
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
            <div style={{ fontSize: '11px', color: '#7a776d' }}>
              Last briefing <span style={{ color: '#98958c' }}>{lastBriefingTime || '—'}</span>
            </div>
            {briefingError && (
              <div style={{ fontSize: '11px', color: '#fb7185' }}>Briefing failed — check connection</div>
            )}
          </div>
        }
      />

      {staleCount > 0 && (
        <div style={{ padding: '10px 14px', borderRadius: '6px', border: '1px solid rgba(232,196,104,0.25)', background: 'rgba(232,196,104,0.06)', color: '#f0d896', fontSize: '12px' }}>
          {staleCount} cached state item{staleCount === 1 ? '' : 's'} stale or unavailable. Last known values remain visible while background workers retry.
        </div>
      )}

      {/* KPI Row */}
      <section style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--gap)' }}>

        {/* Weather */}
        {weather && (
          <Card style={{ flex: '2.2 1 300px', position: 'relative', overflow: 'hidden' }}>
            <div style={{ position: 'absolute', top: '-30px', right: '-20px', width: '160px', height: '160px', borderRadius: '50%', background: 'radial-gradient(circle,rgba(232,196,104,0.16),transparent 70%)' }} />
            <Eyebrow>Environment</Eyebrow>
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginTop: '16px' }}>
              <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="#e8c468" strokeWidth="1.6">
                <circle cx="12" cy="12" r="4.2"/>
                <path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>
              </svg>
              <div>
                <div style={{ fontSize: '38px', fontWeight: 700, lineHeight: 1, letterSpacing: '-0.02em' }}>
                  {weather.temp_f}°
                </div>
                <div style={{ fontSize: '13px', color: '#98958c', marginTop: '5px' }}>
                  {weather.summary || weather.condition}
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '26px', marginTop: '18px', paddingTop: '16px', borderTop: '1px solid rgba(180,178,170,0.10)' }}>
              <div>
                <div style={{ fontSize: '11px', color: '#7a776d', letterSpacing: '0.06em' }}>HIGH / LOW</div>
                <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '3px' }}>
                  {weather.high_f}° / {weather.low_f}°
                </div>
              </div>
              <div>
                <div style={{ fontSize: '11px', color: '#7a776d', letterSpacing: '0.06em' }}>WIND</div>
                <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '3px' }}>
                  {weather.wind_mph} mph
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* Blocked */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>Blocked</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700 }}>
              {adguard?.blocked_pct || 0}<span style={{ fontSize: '17px', color: '#7a776d', fontWeight: 500 }}>%</span>
            </div>
            <div style={{ fontSize: '12px', color: '#98958c', marginTop: '4px' }}>
              {adguard?.blocked_today} today
            </div>
            <div style={{ height: '4px', borderRadius: '3px', background: 'rgba(180,178,170,0.12)', marginTop: '8px', overflow: 'hidden' }}>
              <div style={{ width: `${adguard?.blocked_pct || 0}%`, height: '100%', background: 'var(--accent)', borderRadius: '3px' }} />
            </div>
          </div>
        </Card>

        {/* DVR Storage */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>DVR Storage</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700 }}>
              {pct}<span style={{ fontSize: '17px', color: '#7a776d', fontWeight: 500 }}>%</span>
            </div>
            <div style={{ fontSize: '12px', color: '#98958c', marginTop: '4px' }}>
              {(channels?.storage_used_gb / 1000 || 0).toFixed(2)} / {(channels?.storage_total_gb / 1000 || 0).toFixed(2)} TB
            </div>
            <div style={{ height: '4px', borderRadius: '3px', background: 'rgba(180,178,170,0.12)', marginTop: '8px', overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: '#ff8a3d', borderRadius: '3px' }} />
            </div>
          </div>
        </Card>

        {/* Brain Queue */}
        <Card style={{ flex: '1 1 150px', display: 'flex', flexDirection: 'column', gap: '14px', justifyContent: 'space-between' }}>
          <Eyebrow>Brain Queue</Eyebrow>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 700, color: '#e8c468' }}>
              {brain?.pending || 0}
            </div>
            <div style={{ fontSize: '12px', color: '#98958c', marginTop: '4px' }}>items pending</div>
          </div>
        </Card>

        {/* Claude Usage -- staleness here is NORMAL (the statusline capture
            file only updates while an interactive Claude Code session is
            running), not an outage, so this card dims rather than showing an
            OFFLINE-style colored pill the way integration cards do.
            Dimming must key off the CAPTURE's own age (captured_at), not
            stateFreshness.claude_usage -- that field only reports how
            recently NEXUS polled the file (ttl_seconds=120), which stays
            'fresh' indefinitely as long as the poll itself keeps succeeding,
            even when the underlying capture is days old. A real collector
            failure (stale/unavailable/never_observed) is still its own,
            separately-shown case. */}
        {(() => {
          const collectorDown = stateFreshness.claude_usage && stateFreshness.claude_usage !== 'fresh'
          const captureStale = claudeUsage?.available && claudeUsage?.captured_at != null
            && (Date.now() / 1000 - claudeUsage.captured_at) > 1800
          const dim = collectorDown || captureStale
          return (
        <Card style={{
          flex: '1 1 220px', display: 'flex', flexDirection: 'column', gap: '10px',
          opacity: dim ? 0.6 : 1,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px' }}>
            <Eyebrow>Claude Usage</Eyebrow>
            {dim && (
              <StatusPill tone="grey" label={collectorDown ? String(stateFreshness.claude_usage).toUpperCase() : 'STALE'} />
            )}
          </div>
          {!claudeUsage?.available ? (
            <div style={{ fontSize: '12px', color: '#98958c' }}>No Claude Code session captured yet.</div>
          ) : (
            <>
              {[
                { label: '5-HOUR', w: claudeUsage.five_hour },
                { label: '7-DAY', w: claudeUsage.seven_day },
              ].map(({ label, w }) => (
                <div key={label}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                    <span style={{ fontSize: '11px', color: '#7a776d' }}>{label}</span>
                    <span style={{ fontSize: '13px', fontWeight: 600 }}>
                      {w?.used_percentage != null ? `${Math.round(w.used_percentage)}%` : '—'}
                    </span>
                  </div>
                  <div style={{ height: '4px', borderRadius: '3px', background: 'rgba(180,178,170,0.12)', marginTop: '4px', overflow: 'hidden' }}>
                    <div style={{
                      width: `${w?.used_percentage ?? 0}%`, height: '100%', borderRadius: '3px',
                      background: usageBarColor(w?.used_percentage ?? 0),
                    }} />
                  </div>
                  <div style={{ fontSize: '10px', color: '#7a776d', marginTop: '3px' }}>
                    {w?.resets_at != null ? fmtCountdown(w.resets_at) : 'no data'}
                  </div>
                </div>
              ))}
              <div style={{ fontSize: '10px', color: '#7a776d' }}>captured {fmtAgo(claudeUsage.captured_at)}</div>
            </>
          )}
        </Card>
          )
        })()}

        {/* OpenRouter -- a real live source (source.openrouter already covers
            connectivity on the Sources card below); this card is purely the
            credit/usage numbers, so it dims only on a genuine staleness/
            outage, not by default like the Claude Usage card above. */}
        <Card style={{ flex: '1 1 220px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px' }}>
            <Eyebrow>OpenRouter</Eyebrow>
            {openrouter?.is_free_tier && <StatusPill tone="grey" label="Free tier" />}
          </div>
          {!openrouter?.available ? (
            <div style={{ fontSize: '12px', color: '#98958c' }}>OpenRouter data unavailable.</div>
          ) : (
            <>
              {/* Real account balance (GET /api/v1/credits) leads the card --
                  this is "the balance" in the everyday sense, distinct from
                  and can be much larger than the per-key cap below
                  (live-verified: a key's limit_remaining read $0/exhausted
                  while the account itself held $12.65 of real balance). */}
              {openrouter.account_balance != null && openrouter.account_total_credits != null ? (
                <>
                  <div style={{ fontSize: '20px', fontWeight: 700 }}>
                    ${openrouter.account_balance.toFixed(2)}
                    <span style={{ fontSize: '12px', color: '#98958c', fontWeight: 500 }}> / ${openrouter.account_total_credits.toFixed(2)} balance</span>
                  </div>
                  <div style={{ height: '4px', borderRadius: '3px', background: 'rgba(180,178,170,0.12)', overflow: 'hidden' }}>
                    {(() => {
                      // Guard the divisor: a never-topped-up account can
                      // legitimately have total_credits=0, which would
                      // otherwise divide to NaN -- an invalid CSS width that
                      // silently renders as a full green bar instead of the
                      // "nothing to show" state it actually is.
                      const pct = openrouter.account_total_credits > 0
                        ? Math.max(0, 100 - (openrouter.account_balance / openrouter.account_total_credits) * 100)
                        : 0
                      const clamped = Math.min(100, pct)
                      return <div style={{ width: `${clamped}%`, height: '100%', borderRadius: '3px', background: usageBarColor(clamped) }} />
                    })()}
                  </div>
                </>
              ) : (
                <div style={{ fontSize: '13px', color: '#98958c' }}>Balance unknown</div>
              )}

              {/* Per-key spending cap, secondary -- omitted entirely for an
                  unlimited key (nothing meaningful beyond usage, already
                  covered by the balance above). */}
              {openrouter.credit_limit != null && (
                <div style={{ fontSize: '11px', color: '#98958c' }}>
                  Key limit: {openrouter.credit_remaining != null ? `$${openrouter.credit_remaining.toFixed(2)}` : 'unknown'} / ${openrouter.credit_limit.toFixed(2)}
                </div>
              )}
              <div style={{ fontSize: '11px', color: '#98958c' }}>{openrouter.model_count} models available</div>
            </>
          )}
        </Card>
      </section>

      {/* System Sources */}
      <Card>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: '10px', marginBottom: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px' }}>
            <Eyebrow>System Sources</Eyebrow>
            <span style={{ fontSize: '12px', color: '#5fe0b4', fontWeight: 500 }}>{online} connected</span>
          </div>
          <span style={{ fontSize: '11px', color: '#7a776d' }}>Synced {syncedTime || '—'}</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px' }}>
          {Object.entries(sources || {}).map(([name, data]) => (
            <div key={name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', padding: '13px 14px', borderRadius: '6px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(180,178,170,0.08)' }}>
              <span style={{ fontSize: '13px', fontWeight: 600, color: '#ece9e2', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: 'none' }}>
                <StatusDot color={data.freshness !== 'fresh' ? '#e8c468' : data.healthy ? '#34d399' : '#fb7185'} size={7} glow={false} />
                <span style={{ fontSize: '10px', letterSpacing: '0.08em', fontWeight: 600, color: data.freshness !== 'fresh' ? '#f0d896' : data.healthy ? '#5fe0b4' : '#fb7185' }}>
                  {data.freshness !== 'fresh' ? String(data.freshness || 'UNKNOWN').toUpperCase() : data.healthy ? 'ONLINE' : 'OFFLINE'}
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
              tone={adguard?.filtering_enabled == null ? 'amber' : adguard.filtering_enabled ? 'green' : 'grey'}
              label={adguard?.filtering_enabled == null ? 'Unknown' : adguard.filtering_enabled ? 'Filtering on' : 'Off'}
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
          <div style={{ fontSize: '13px', color: '#98958c', marginTop: '7px' }}>
            queries blocked of {adguard?.queries_today} total today
          </div>
          <div style={{ height: '8px', borderRadius: '4px', background: 'rgba(180,178,170,0.12)', marginTop: '18px', overflow: 'hidden' }}>
            <div style={{ width: `${adguard?.blocked_pct || 0}%`, height: '100%', background: 'linear-gradient(90deg,var(--accent),#c96a2e)' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '14px', flexWrap: 'wrap', marginTop: '18px', paddingTop: '16px', borderTop: '1px solid rgba(180,178,170,0.10)' }}>
            <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap' }}>
              <div>
                <div style={{ fontSize: '11px', color: '#7a776d' }}>QUERIES</div>
                <div style={{ fontSize: '16px', fontWeight: 600, marginTop: '3px' }}>{adguard?.queries_today}</div>
              </div>
              <div>
                <div style={{ fontSize: '11px', color: '#7a776d' }}>BLOCKED</div>
                <div style={{ fontSize: '16px', fontWeight: 600, marginTop: '3px' }}>{adguard?.blocked_today}</div>
              </div>
              <div>
                <div style={{ fontSize: '11px', color: '#7a776d' }}>ALLOWED</div>
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
              <circle cx="46" cy="46" r="38" fill="none" stroke="rgba(180,178,170,0.14)" strokeWidth="9"/>
              <circle cx="46" cy="46" r="38" fill="none" stroke="#ff8a3d" strokeWidth="9" strokeLinecap="round"
                strokeDasharray="238.76" strokeDashoffset={238.76 * (1 - pct / 100)} transform="rotate(-90 46 46)"/>
              <text x="46" y="50" textAnchor="middle" fill="#f4f3f0" fontSize="20" fontWeight="700" fontFamily="Archivo">{pct}%</text>
            </svg>
            <div>
              <div style={{ fontSize: '13px', color: '#98958c' }}>
                {channels?.recording_now?.length
                  ? `${channels.recording_now.length} recording${channels.recording_now.length !== 1 ? 's' : ''}`
                  : 'No active recordings'}
              </div>
              <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '10px' }}>
                {(channels?.storage_used_gb / 1000 || 0).toFixed(2)} TB <span style={{ color: '#7a776d', fontWeight: 500 }}>used</span>
              </div>
              <div style={{ fontSize: '12px', color: '#7a776d', marginTop: '3px' }}>
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
              <div style={{ fontSize: '13px', color: '#98958c' }}>containers running</div>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {(unraid.docker_containers || []).slice(0, dockerOpen ? undefined : 2).map(c => (
                <div
                  key={c.id}
                  onClick={() => restartDocker(c.name)}
                  style={{ flex: '1 1 45%', display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '6px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(180,178,170,0.08)', cursor: 'pointer' }}
                >
                  <StatusDot color="#34d399" size={7} glow={false} />
                  <span style={{ fontSize: '12px', color: '#d9d6cd', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {c.name || 'Up'}
                  </span>
                </div>
              ))}
              {(unraid.docker_containers?.length || 0) === 0 && (
                <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '6px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(180,178,170,0.08)' }}>
                  <span style={{ fontSize: '12px', color: '#7a776d', fontWeight: 500 }}>No containers running</span>
                </div>
              )}
            </div>
            {(unraid.docker_containers?.length || 0) > 2 && (
              <button
                onClick={() => setDockerOpen(v => !v)}
                style={{ fontSize: '11px', fontWeight: 600, color: '#7a776d', background: 'none', border: 'none', cursor: 'pointer', padding: '10px 0', textAlign: 'left' }}
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
              <div style={{ fontSize: '13px', color: '#98958c' }}>VMs / containers</div>
            </div>
            {proxmoxMaint && (proxmoxMaint.updates?.count > 0 || (proxmoxMaint.backup && proxmoxMaint.backup.status !== 'none')) && (
              <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
                {proxmoxMaint.updates?.count > 0 && (
                  <StatusPill
                    tone="amber"
                    label={`${proxmoxMaint.updates.count} update${proxmoxMaint.updates.count === 1 ? '' : 's'} pending`}
                  />
                )}
                {proxmoxMaint.backup && proxmoxMaint.backup.status !== 'none' && (
                  <>
                    <StatusPill
                      tone={proxmoxMaint.backup.status === 'ok' ? 'green' : proxmoxMaint.backup.status === 'failed' ? 'red' : 'grey'}
                      label={proxmoxMaint.backup.status === 'ok' ? 'Backup OK' : proxmoxMaint.backup.status === 'failed' ? 'Backup FAILED' : 'Backup running'}
                    />
                    {proxmoxMaint.backup.endtime && (
                      <span style={{ fontSize: '11px', color: '#7a776d' }}>
                        {new Date(proxmoxMaint.backup.endtime * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}
                      </span>
                    )}
                  </>
                )}
              </div>
            )}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {(proxmox.vms || []).slice(0, proxmoxVmsOpen ? undefined : 4).map(v => (
                <div
                  key={v.vmid}
                  style={{ flex: '1 1 45%', display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '6px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(180,178,170,0.08)' }}
                >
                  <StatusDot color={v.status === 'running' ? '#34d399' : '#98958c'} size={7} glow={false} />
                  <span style={{ fontSize: '12px', color: '#d9d6cd', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                    {v.name || v.vmid}
                  </span>
                  <select
                    value=""
                    disabled={vmActionBusy === v.vmid}
                    onChange={(e) => { const action = e.target.value; e.target.value = ''; if (action) runVmAction(v.vmid, v.name, action) }}
                    style={{ fontSize: '12px', background: 'rgba(255,255,255,0.04)', color: '#98958c', border: '1px solid rgba(180,178,170,0.12)', borderRadius: '4px', padding: '7px 8px' }}
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
                style={{ fontSize: '11px', fontWeight: 600, color: '#7a776d', background: 'none', border: 'none', cursor: 'pointer', padding: '10px 0', textAlign: 'left' }}
              >
                {proxmoxVmsOpen ? 'Show less' : `+${proxmox.vms.length - 4} more`}
              </button>
            )}
          </Card>
        )}

        {/* Proton Mail */}
        {(mail || mailError) && (
          <Card style={{ flex: '1.2 1 300px', display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <Eyebrow>Proton Mail</Eyebrow>
              {mailError ? (
                <StatusPill tone="red" label="Offline" />
              ) : (
                <StatusPill
                  tone={mail.unread > 0 ? 'amber' : 'green'}
                  label={mail.unread > 0 ? `${mail.unread} unread` : 'Caught up'}
                />
              )}
            </div>
            {!mailError && (
              <>
                <div style={{ fontSize: '12px', color: '#98958c', margin: '8px 0' }}>
                  {mail.total} total in inbox
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {(mail.emails || []).slice(0, mailOpen ? undefined : 3).map(e => (
                    <div
                      key={e.email_id}
                      style={{ display: 'flex', flexDirection: 'column', padding: '8px 10px', borderRadius: '6px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(180,178,170,0.08)' }}
                    >
                      <span style={{ fontSize: '12px', color: '#d9d6cd', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {(e.sender || '').replace(/^"?([^"<]+?)"?\s*<.*$/, '$1')}
                      </span>
                      <span style={{ fontSize: '11px', color: '#98958c', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {e.subject || '(no subject)'}
                      </span>
                    </div>
                  ))}
                  {(mail.emails?.length || 0) === 0 && (
                    <div style={{ fontSize: '12px', color: '#7a776d' }}>No emails.</div>
                  )}
                </div>
                {(mail.emails?.length || 0) > 3 && (
                  <button
                    onClick={() => setMailOpen(v => !v)}
                    style={{ fontSize: '11px', fontWeight: 600, color: '#7a776d', background: 'none', border: 'none', cursor: 'pointer', padding: '10px 0', textAlign: 'left' }}
                  >
                    {mailOpen ? 'Show less' : `+${mail.emails.length - 3} more`}
                  </button>
                )}
              </>
            )}
          </Card>
        )}
      </section>
    </div>
  )
}
