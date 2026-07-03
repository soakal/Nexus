import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { api, API_BASE } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'
import GhostButton from '../components/GhostButton'
import TextInput from '../components/TextInput'

// Proxmox VMs/LXCs — hardcoded from NEXUS CLAUDE.md
const PROXMOX_VMS = [
  'Win11Pro', 'MintLinux', 'Win11ProTrudy',
  'Hermes', 'AdGuard', 'Jellyfin',
]

function ProxmoxSection() {
  const [pending, setPending] = useState({})  // { name: 'start'|'stop'|'reboot'|null }
  const [toast, setToast] = useState(null)    // { msg, ok }
  const toastTimer = useRef(null)

  const showToast = (msg, ok) => {
    clearTimeout(toastTimer.current)
    setToast({ msg, ok })
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }

  const sendCmd = async (name, action) => {
    setPending((p) => ({ ...p, [name]: action }))
    try {
      const key = localStorage.getItem('nexus_api_key') || ''
      const message = `${action} ${name}`
      const res = await fetch(`${API_BASE}/api/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` },
        body: JSON.stringify({ message }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      // Drain the stream so the command fully executes
      const reader = res.body.getReader()
      let reply = ''
      const dec = new TextDecoder()
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        reply += dec.decode(value, { stream: true })
      }
      showToast(`${action} ${name}: sent`, true)
    } catch (e) {
      showToast(`${action} ${name}: ${e.message}`, false)
    } finally {
      setPending((p) => { const n = { ...p }; delete n[name]; return n })
    }
  }

  const btnStyle = (variant) => ({
    padding: '4px 10px',
    borderRadius: '7px',
    fontSize: '11px',
    fontWeight: 600,
    cursor: 'pointer',
    border: variant === 'start'
      ? '1px solid rgba(52,211,153,0.3)'
      : variant === 'stop'
      ? '1px solid rgba(251,113,133,0.3)'
      : '1px solid rgba(120,160,220,0.2)',
    background: variant === 'start'
      ? 'rgba(52,211,153,0.08)'
      : variant === 'stop'
      ? 'rgba(251,113,133,0.08)'
      : 'rgba(255,255,255,0.04)',
    color: variant === 'start' ? '#34d399' : variant === 'stop' ? '#fb7185' : '#aab4c7',
  })

  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <Eyebrow>Proxmox VMs / LXCs</Eyebrow>
        {toast && (
          <span style={{
            fontSize: '11px', fontWeight: 600, padding: '3px 10px', borderRadius: '6px',
            background: toast.ok ? 'rgba(52,211,153,0.1)' : 'rgba(251,113,133,0.1)',
            color: toast.ok ? '#34d399' : '#fb7185',
            border: toast.ok ? '1px solid rgba(52,211,153,0.25)' : '1px solid rgba(251,113,133,0.25)',
          }}>
            {toast.msg}
          </span>
        )}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {PROXMOX_VMS.map((name) => {
          const busy = !!pending[name]
          return (
            <div key={name} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
              padding: '10px 14px', borderRadius: '11px',
              background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)',
            }}>
              <span style={{ fontSize: '14px', fontWeight: 600, color: '#dbe3f0' }}>{name}</span>
              <div style={{ display: 'flex', gap: '6px' }}>
                {['start', 'stop', 'reboot'].map((action) => (
                  <button
                    key={action}
                    disabled={busy}
                    onClick={() => sendCmd(name, action)}
                    style={{
                      ...btnStyle(action),
                      opacity: busy ? 0.5 : 1,
                      cursor: busy ? 'not-allowed' : 'pointer',
                    }}
                  >
                    {busy && pending[name] === action ? '…' : action}
                  </button>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

// Curated controls — the ONLY entities this tab shows, in this order, grouped
// under human category names. Everything else in HA is still controllable by
// name via Chat; add a row here to surface a device.
const CONTROLS = [
  { id: 'light.left_porch_light',                  group: 'Lights', name: 'Left Porch Light' },
  { id: 'light.right_porch_light',                 group: 'Lights', name: 'Right Porch Light' },
  { id: 'light.left_garage_light',                 group: 'Lights', name: 'Left Garage Light' },
  { id: 'light.right_garage_light',                group: 'Lights', name: 'Right Garage Light' },
  { id: 'light.tall_light_lr_christmas_tree_plug', group: 'Lights', name: 'Living Room Tall Light' },
  { id: 'light.table_light_lr',                    group: 'Lights', name: 'Living Room Table Light' },
  { id: 'switch.basement_lights',                  group: 'Lights', name: 'Basement Lights' },
  { id: 'light.trudy_bedroom_light',               group: 'Lights', name: 'Trudy Bedroom Light' },
  { id: 'cover.garage_door_garage_door',           group: 'Doors & Garage', name: 'Garage Door' },
  { id: 'lock.dining_room',                        group: 'Doors & Garage', name: 'Back Door Lock' },
  { id: 'climate.dining_room',                     group: 'Thermostat', name: 'Ecobee' },
  { id: 'switch.tp_link_power_strip_3c86_mb_fan',  group: 'Fans', name: 'Master Bedroom Fan' },
]

const CONTROL_META = new Map(CONTROLS.map((c, i) => [c.id, { ...c, order: i }]))
const GROUP_ORDER = [...new Set(CONTROLS.map((c) => c.group))]

const TOGGLE_DOMAINS = new Set(['light', 'switch', 'fan', 'input_boolean'])
const ON_STATES = new Set(['on', 'open', 'home', 'playing', 'active', 'unlocked'])

// Do NOT compensate for the Ecobee's +3°F during 3-7pm: that's Brian's
// electric-utility peak-savings program raising the cool setpoint on purpose.
// Sending lower values to cancel it would forfeit the peak-rate savings.
// ponytail: knob kept at 0 — only change if a genuine device offset appears.
const ECOBEE_SET_OFFSET = 0

const MODE_LABEL = { off: 'Off', heat: 'Heat', cool: 'Cool', heat_cool: 'Auto' }
const MODE_COLOR = { off: '#7c8aa3', heat: '#f97316', cool: '#38bdf8', heat_cool: '#a78bfa' }

function domainOf(entityId) {
  return entityId.split('.')[0]
}

function friendlyName(entity) {
  return CONTROL_META.get(entity.entity_id)?.name || entity.attributes?.friendly_name || entity.entity_id
}

function isOn(state) {
  return ON_STATES.has((state || '').toLowerCase())
}

function Toggle({ on, disabled, onClick }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-pressed={on}
      style={{
        width: '46px', height: '26px', borderRadius: '13px', padding: '2px', flexShrink: 0,
        border: on ? '1px solid rgba(52,211,153,0.5)' : '1px solid rgba(120,160,220,0.2)',
        background: on ? 'rgba(52,211,153,0.35)' : 'rgba(255,255,255,0.06)',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.4 : 1,
        transition: 'background 0.15s',
      }}
    >
      <div style={{
        width: '20px', height: '20px', borderRadius: '50%',
        background: on ? '#34d399' : '#8a96ad',
        transform: on ? 'translateX(20px)' : 'translateX(0)',
        transition: 'transform 0.15s',
      }} />
    </button>
  )
}

const ROW = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
  padding: '12px 14px', borderRadius: '11px',
  background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)',
}
const NAME = {
  fontSize: '14px', fontWeight: 600, color: '#dbe3f0',
  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
}
const BIG_BTN = (disabled) => ({
  padding: '8px 22px', borderRadius: '9px', fontSize: '13px', fontWeight: 700,
  border: '1px solid rgba(120,160,220,0.22)', background: 'rgba(255,255,255,0.05)',
  color: '#dbe3f0', cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.4 : 1,
})

function EntityRow({ entity, onAction, busy }) {
  const domain = domainOf(entity.entity_id)
  const name = friendlyName(entity)
  const state = entity.state
  const on = isOn(state)
  const unavailable = state === 'unavailable' || state === 'unknown'

  // Lights, fan, plugs: a switch you flip
  if (TOGGLE_DOMAINS.has(domain)) {
    return (
      <div style={ROW}>
        <div style={{ ...NAME, minWidth: 0, flex: 1 }}>{name}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
          <span style={{
            fontSize: '12px', fontWeight: 600,
            color: unavailable ? '#f4d27a' : on ? '#5fe0b4' : '#7c8aa3',
          }}>
            {unavailable ? 'N/A' : on ? 'On' : 'Off'}
          </span>
          <Toggle
            on={on}
            disabled={busy || unavailable}
            onClick={() => onAction(entity, on ? 'turn_off' : 'turn_on', null, { state: on ? 'off' : 'on' })}
          />
        </div>
      </div>
    )
  }

  // Garage door: big OPEN/CLOSED status + one action button
  if (domain === 'cover') {
    const moving = state === 'opening' || state === 'closing'
    const isOpen = state === 'open'
    const statusColor = unavailable ? '#f4d27a' : moving ? '#fbbf24' : isOpen ? '#fb7185' : '#34d399'
    const action = moving
      ? { label: 'Stop', svc: 'stop_cover', opt: null }
      : isOpen
        ? { label: 'Close', svc: 'close_cover', opt: { state: 'closing' } }
        : { label: 'Open', svc: 'open_cover', opt: { state: 'opening' } }
    return (
      <div style={ROW}>
        <div style={{ ...NAME, minWidth: 0, flex: 1 }}>{name}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexShrink: 0 }}>
          <span style={{ fontSize: '13px', fontWeight: 700, letterSpacing: '1px', color: statusColor }}>
            {unavailable ? 'N/A' : state.toUpperCase()}
          </span>
          <button
            disabled={busy || unavailable}
            onClick={() => onAction(entity, action.svc, null, action.opt)}
            style={BIG_BTN(busy || unavailable)}
          >
            {action.label}
          </button>
        </div>
      </div>
    )
  }

  // Back door lock: LOCKED/UNLOCKED status + one action button
  if (domain === 'lock') {
    const isLocked = state === 'locked'
    const statusColor = unavailable ? '#f4d27a' : isLocked ? '#34d399' : '#fb7185'
    return (
      <div style={ROW}>
        <div style={{ ...NAME, minWidth: 0, flex: 1 }}>{name}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexShrink: 0 }}>
          <span style={{ fontSize: '13px', fontWeight: 700, letterSpacing: '1px', color: statusColor }}>
            {unavailable ? 'N/A' : isLocked ? 'LOCKED' : 'UNLOCKED'}
          </span>
          <button
            disabled={busy || unavailable}
            onClick={() => onAction(
              entity,
              isLocked ? 'unlock' : 'lock',
              null,
              { state: isLocked ? 'unlocked' : 'locked' },
            )}
            style={BIG_BTN(busy || unavailable)}
          >
            {isLocked ? 'Unlock' : 'Lock'}
          </button>
        </div>
      </div>
    )
  }

  // Thermostat: Ecobee-style panel — big setpoint, up/down, mode pills
  if (domain === 'climate') {
    const attrs = entity.attributes || {}
    const modes = attrs.hvac_modes || ['off', 'heat', 'cool']
    const target = attrs.temperature
    const current = attrs.current_temperature
    const humidity = attrs.current_humidity
    const hvacAction = attrs.hvac_action
    const mc = unavailable ? '#f4d27a' : MODE_COLOR[state] || '#7c8aa3'
    // ponytail: single-setpoint only — heat_cool (Auto) needs high/low targets, ± disabled there
    const canSetTemp = !unavailable && !busy && target != null && state !== 'off' && state !== 'heat_cool'
    const setTemp = (t) => onAction(
      entity, 'set_temperature',
      { temperature: t - ECOBEE_SET_OFFSET },
      { attrs: { temperature: t } },
    )
    const chev = (disabled) => ({
      width: '52px', height: '38px', borderRadius: '10px', fontSize: '15px', fontWeight: 700,
      border: '1px solid rgba(120,160,220,0.2)', background: 'rgba(255,255,255,0.05)',
      color: '#dbe3f0', cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.35 : 1,
    })
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px',
        padding: '22px 14px', borderRadius: '14px',
        background: 'rgba(255,255,255,0.022)', border: `1px solid ${mc}55`,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '22px' }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: '58px', fontWeight: 700, lineHeight: 1, color: mc }}>
              {unavailable ? '--' : (state === 'off' ? (current ?? '--') : (target ?? current ?? '--'))}°
            </div>
            <div style={{ fontSize: '11px', letterSpacing: '2px', color: '#8a96ad', marginTop: '8px', textTransform: 'uppercase' }}>
              {unavailable ? 'Unavailable' : state === 'off' ? 'System Off' : 'Set To'}
            </div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <button disabled={!canSetTemp} onClick={() => setTemp(target + 1)} style={chev(!canSetTemp)}>▲</button>
            <button disabled={!canSetTemp} onClick={() => setTemp(target - 1)} style={chev(!canSetTemp)}>▼</button>
          </div>
        </div>
        <div style={{ fontSize: '13px', color: '#9aa6bd' }}>
          Inside <strong style={{ color: '#dbe3f0' }}>{current ?? '--'}°</strong>
          {humidity != null && <> · Humidity <strong style={{ color: '#dbe3f0' }}>{Math.round(humidity)}%</strong></>}
          {hvacAction && <> · {hvacAction}</>}
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center' }}>
          {modes.map((m) => {
            const active = state === m
            const col = MODE_COLOR[m] || '#7c8aa3'
            return (
              <button
                key={m}
                disabled={busy || unavailable || active}
                onClick={() => onAction(entity, 'set_hvac_mode', { hvac_mode: m }, { state: m })}
                style={{
                  padding: '7px 18px', borderRadius: '999px', fontSize: '13px', fontWeight: 700,
                  border: active ? `1px solid ${col}` : '1px solid rgba(120,160,220,0.2)',
                  background: active ? `${col}22` : 'rgba(255,255,255,0.04)',
                  color: active ? col : '#9aa6bd',
                  cursor: busy || unavailable || active ? 'default' : 'pointer',
                  opacity: busy || unavailable ? 0.4 : 1,
                }}
              >
                {MODE_LABEL[m] || m}
              </button>
            )
          })}
        </div>
      </div>
    )
  }

  // Anything else: read-only state display
  const unit = entity.attributes?.unit_of_measurement || ''
  return (
    <div style={ROW}>
      <div style={{ ...NAME, minWidth: 0, flex: 1 }}>{name}</div>
      <div style={{ flexShrink: 0, fontSize: '12px', fontWeight: 600, color: '#9aa6bd' }}>
        {state}{unit ? ` ${unit}` : ''}
      </div>
    </div>
  )
}

export default function HomeAssistant() {
  const [entities, setEntities] = useState([])
  const [alerts, setAlerts] = useState([])
  const [cloudAlerts, setCloudAlerts] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [reloading, setReloading] = useState(false)
  const [filter, setFilter] = useState('')
  const [busyIds, setBusyIds] = useState({})

  const load = useCallback(async () => {
    try {
      const data = await api.ha.entities()
      setEntities((data.entities || []).filter((e) => CONTROL_META.has(e.entity_id)))
      setAlerts(data.alerts || [])
      setCloudAlerts(data.cloud_alerts || [])
      setError(null)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }, [])

  const reloadCloud = async () => {
    setReloading(true)
    try { await api.post('/ha/reload-cloud'); await load() } catch {} finally { setReloading(false) }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 10000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load])

  const callService = useCallback(async (entity, service, serviceData, optimistic) => {
    const domain = domainOf(entity.entity_id)
    setBusyIds((b) => ({ ...b, [entity.entity_id]: true }))
    if (optimistic && (optimistic.state != null || optimistic.attrs)) {
      setEntities((list) =>
        list.map((e) =>
          e.entity_id === entity.entity_id
            ? {
                ...e,
                ...(optimistic.state != null ? { state: optimistic.state } : {}),
                ...(optimistic.attrs ? { attributes: { ...e.attributes, ...optimistic.attrs } } : {}),
              }
            : e
        )
      )
    }
    try {
      await api.ha.service(domain, service, entity.entity_id, serviceData)
      // HA's state machine lags the service call for polled devices (TP-Link,
      // ESPHome) — reloading instantly reverts the optimistic state and the
      // button visibly snaps back, which reads as "the control didn't work".
      // Setpoint writes lag even longer (Ecobee cloud applies its +3 after a
      // few seconds), so skip the reload there and let the 10s poll confirm.
      if (service !== 'set_temperature') {
        await new Promise((r) => setTimeout(r, 1800))
        await load()
      }
    } catch (e) {
      setError(String(e.message || e))
      await load()
    } finally {
      setBusyIds((b) => { const next = { ...b }; delete next[entity.entity_id]; return next })
    }
  }, [load])

  const grouped = useMemo(() => {
    const q = filter.trim().toLowerCase()
    const groups = {}
    for (const e of entities) {
      if (q) {
        const hay = `${e.entity_id} ${friendlyName(e)}`.toLowerCase()
        if (!hay.includes(q)) continue
      }
      const g = CONTROL_META.get(e.entity_id)?.group || 'Other'
      if (!groups[g]) groups[g] = []
      groups[g].push(e)
    }
    for (const g of Object.keys(groups)) {
      groups[g].sort((a, b) =>
        (CONTROL_META.get(a.entity_id)?.order ?? 999) - (CONTROL_META.get(b.entity_id)?.order ?? 999))
    }
    return groups
  }, [entities, filter])

  const domains = useMemo(
    () => GROUP_ORDER.filter((g) => grouped[g]).concat(grouped.Other ? ['Other'] : []),
    [grouped])

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
      <ScreenHeader
        section="Home Assistant"
        title="Home Systems"
        right={
          <div style={{ display: 'flex', flexDirection: 'row', gap: '14px', flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ fontSize: '13px', color: '#8a96ad' }}>
              <strong style={{ color: '#e9eef8' }}>{entities?.length || 0}</strong>
              {' '}entities · <strong style={{ color: '#fbbf24' }}>{alerts?.length || 0}</strong> unavailable
            </span>
            <GhostButton onClick={reloadCloud} disabled={reloading}>
              {reloading ? 'Reloading…' : 'Reload cloud'}
            </GhostButton>
          </div>
        }
      />

      <TextInput
        style={{ width: '100%' }}
        type="text"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter by name or entity id…"
      />

      {cloudAlerts.length > 0 && (
        <Card accent="amber">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
            <StatusDot color="#fbbf24" size={8} glow={false} />
            <Eyebrow style={{ color: '#f4d27a' }}>HA Cloud Alert</Eyebrow>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {cloudAlerts.map((ca) => (
              <div key={ca.entity} style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: '12px' }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: '13px', color: '#dbe3f0' }}>{ca.message}</div>
                  <div style={{ fontSize: '11px', fontFamily: "'JetBrains Mono', monospace", color: '#8a96ad', marginTop: '3px' }}>
                    {ca.entity} · <span style={{ color: '#f4d27a' }}>{ca.state}</span>
                  </div>
                </div>
                <GhostButton onClick={reloadCloud} disabled={reloading}>
                  {reloading ? 'Reloading…' : 'Reload cloud'}
                </GhostButton>
              </div>
            ))}
          </div>
        </Card>
      )}

      {error && (
        <div style={{
          borderRadius: '16px',
          padding: 'var(--pad)',
          border: '1px solid rgba(251,113,133,0.3)',
          background: 'rgba(251,113,133,0.05)',
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
        }}>
          <StatusDot color="#fb7185" size={8} glow={false} />
          <span style={{ fontSize: '13px', color: '#fb7185' }}>{error}</span>
        </div>
      )}

      <ProxmoxSection />

      {loading ? (
        <div style={{ color: '#5d6982', fontSize: '13px' }}>Loading…</div>
      ) : domains.length === 0 ? (
        <div style={{ color: '#8a96ad', fontSize: '13px' }}>No entities match.</div>
      ) : (
        domains.map((domain) => (
          <Card key={domain}>
            <Eyebrow>
              {domain}{' '}
              <span style={{ color: '#465069' }}>({grouped[domain].length})</span>
            </Eyebrow>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '12px' }}>
              {grouped[domain].map((e) => (
                <EntityRow
                  key={e.entity_id}
                  entity={e}
                  onAction={callService}
                  busy={!!busyIds[e.entity_id]}
                />
              ))}
            </div>
          </Card>
        ))
      )}
    </div>
  )
}
