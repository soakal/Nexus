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
const COVER_DOMAINS = new Set(['cover'])
const LOCK_DOMAINS  = new Set(['lock'])
const ON_STATES = new Set(['on', 'open', 'home', 'playing', 'active', 'unlocked'])

const ACTION_BTN = {
  padding: '5px 10px', borderRadius: '7px', fontSize: '12px', fontWeight: 600,
  border: '1px solid rgba(120,160,220,0.2)', background: 'rgba(255,255,255,0.04)',
  color: '#aab4c7', cursor: 'pointer',
}

function domainOf(entityId) {
  return entityId.split('.')[0]
}

function friendlyName(entity) {
  return CONTROL_META.get(entity.entity_id)?.name || entity.attributes?.friendly_name || entity.entity_id
}

function isOn(state) {
  return ON_STATES.has((state || '').toLowerCase())
}

function entityDotColor(state) {
  if (state === 'unavailable' || state === 'unknown') return '#fbbf24'
  if (isOn(state) || state === 'armed') return '#34d399'
  return '#7c8aa3'
}

function EntityRow({ entity, onAction, busy }) {
  const domain = domainOf(entity.entity_id)
  const name = friendlyName(entity)
  const state = entity.state
  const on = isOn(state)
  const unavailable = state === 'unavailable' || state === 'unknown'
  const dotColor = entityDotColor(state)

  const rowStyle = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '12px',
    padding: '12px 14px',
    borderRadius: '11px',
    background: 'rgba(255,255,255,0.022)',
    border: '1px solid rgba(120,160,220,0.08)',
  }

  const nameStyle = {
    fontSize: '14px',
    fontWeight: 600,
    color: '#dbe3f0',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  }

  const idStyle = {
    fontSize: '11px',
    fontFamily: "'JetBrains Mono', monospace",
    color: '#5d6982',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  }

  const leftStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    minWidth: 0,
    flex: 1,
  }

  const labelStyle = {
    fontSize: '12px',
    fontWeight: 600,
    color: unavailable ? '#f4d27a' : on ? '#5fe0b4' : '#9aa6bd',
  }

  if (TOGGLE_DOMAINS.has(domain)) {
    return (
      <div style={rowStyle}>
        <div style={leftStyle}>
          <StatusDot color={dotColor} size={8} glow={false} />
          <div style={{ minWidth: 0 }}>
            <div style={nameStyle}>{name}</div>
          </div>
        </div>
        <button
          disabled={busy || unavailable}
          onClick={() => onAction(entity, on ? 'turn_off' : 'turn_on', on ? 'off' : 'on')}
          style={{
            flexShrink: 0,
            padding: '5px 12px',
            borderRadius: '7px',
            border: unavailable
              ? '1px solid rgba(120,160,220,0.12)'
              : on
              ? '1px solid rgba(95,224,180,0.3)'
              : '1px solid rgba(120,160,220,0.16)',
            background: unavailable
              ? 'rgba(255,255,255,0.03)'
              : on
              ? 'rgba(95,224,180,0.08)'
              : 'rgba(255,255,255,0.03)',
            cursor: busy || unavailable ? 'not-allowed' : 'pointer',
            opacity: busy ? 0.5 : 1,
            ...labelStyle,
          }}
        >
          {unavailable ? 'N/A' : on ? 'On' : 'Off'}
        </button>
      </div>
    )
  }

  if (COVER_DOMAINS.has(domain)) {
    const isMoving = state === 'opening' || state === 'closing'
    const coverBtns = [
      { label: 'Open',  svc: 'open_cover',  off: state === 'open'   || state === 'opening'  || unavailable },
      { label: 'Close', svc: 'close_cover', off: state === 'closed' || state === 'closing'  || unavailable },
      { label: 'Stop',  svc: 'stop_cover',  off: !isMoving || unavailable },
    ]
    return (
      <div style={rowStyle}>
        <div style={leftStyle}>
          <StatusDot color={dotColor} size={8} glow={false} />
          <div style={{ minWidth: 0 }}>
            <div style={nameStyle}>{name}</div>
          </div>
        </div>
        <div style={{ flexShrink: 0, display: 'flex', gap: '6px', alignItems: 'center' }}>
          <span style={{ ...labelStyle, marginRight: '4px' }}>{state}</span>
          {coverBtns.map(({ label, svc, off }) => (
            <button key={svc} disabled={busy || off}
              onClick={() => onAction(entity, svc, null)}
              style={{ ...ACTION_BTN, opacity: busy || off ? 0.35 : 1, cursor: busy || off ? 'not-allowed' : 'pointer' }}
            >{label}</button>
          ))}
        </div>
      </div>
    )
  }

  if (LOCK_DOMAINS.has(domain)) {
    const isLocked = state === 'locked'
    const lockBtns = [
      { label: 'Lock',   svc: 'lock',   opt: 'locked',   off: isLocked  || unavailable },
      { label: 'Unlock', svc: 'unlock', opt: 'unlocked', off: !isLocked || unavailable },
    ]
    return (
      <div style={rowStyle}>
        <div style={leftStyle}>
          <StatusDot color={dotColor} size={8} glow={false} />
          <div style={{ minWidth: 0 }}>
            <div style={nameStyle}>{name}</div>
          </div>
        </div>
        <div style={{ flexShrink: 0, display: 'flex', gap: '6px', alignItems: 'center' }}>
          <span style={{ ...labelStyle, marginRight: '4px' }}>{state}</span>
          {lockBtns.map(({ label, svc, opt, off }) => (
            <button key={svc} disabled={busy || off}
              onClick={() => onAction(entity, svc, opt)}
              style={{ ...ACTION_BTN, opacity: busy || off ? 0.35 : 1, cursor: busy || off ? 'not-allowed' : 'pointer' }}
            >{label}</button>
          ))}
        </div>
      </div>
    )
  }

  if (domain === 'climate') {
    const attrs = entity.attributes || {}
    const modes = attrs.hvac_modes || ['off', 'heat', 'cool']
    const target = attrs.temperature
    const current = attrs.current_temperature
    // ponytail: single-target only — heat_cool needs target_temp_high/low, so ± is disabled there
    const canSetTemp = !unavailable && target != null && state !== 'off' && state !== 'heat_cool'
    return (
      <div style={rowStyle}>
        <div style={leftStyle}>
          <StatusDot color={unavailable ? '#fbbf24' : state === 'off' ? '#7c8aa3' : '#34d399'} size={8} glow={false} />
          <div style={{ minWidth: 0 }}>
            <div style={nameStyle}>{name}</div>
            <div style={idStyle}>
              {current != null ? `${current}° now` : entity.entity_id}
              {attrs.hvac_action ? ` · ${attrs.hvac_action}` : ''}
            </div>
          </div>
        </div>
        <div style={{ flexShrink: 0, display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <button disabled={busy || !canSetTemp}
            onClick={() => onAction(entity, 'set_temperature', null, { temperature: target - 1 })}
            style={{ ...ACTION_BTN, opacity: busy || !canSetTemp ? 0.35 : 1, cursor: busy || !canSetTemp ? 'not-allowed' : 'pointer' }}
          >−</button>
          <span style={labelStyle}>{target != null ? `${target}°` : '—'}</span>
          <button disabled={busy || !canSetTemp}
            onClick={() => onAction(entity, 'set_temperature', null, { temperature: target + 1 })}
            style={{ ...ACTION_BTN, opacity: busy || !canSetTemp ? 0.35 : 1, cursor: busy || !canSetTemp ? 'not-allowed' : 'pointer' }}
          >+</button>
          {modes.map((m) => {
            const active = state === m
            const off = busy || unavailable || active
            return (
              <button key={m} disabled={off}
                onClick={() => onAction(entity, 'set_hvac_mode', m, { hvac_mode: m })}
                style={{
                  ...ACTION_BTN,
                  opacity: busy || unavailable ? 0.35 : 1,
                  cursor: off ? 'not-allowed' : 'pointer',
                  ...(active ? { border: '1px solid rgba(95,224,180,0.3)', background: 'rgba(95,224,180,0.08)', color: '#5fe0b4' } : {}),
                }}
              >{m.replace('_', '/')}</button>
            )
          })}
        </div>
      </div>
    )
  }

  // Sensors and everything else: read-only state display
  const unit = entity.attributes?.unit_of_measurement || ''
  return (
    <div style={rowStyle}>
      <div style={leftStyle}>
        <StatusDot color={dotColor} size={8} glow={false} />
        <div style={{ minWidth: 0 }}>
          <div style={nameStyle}>{name}</div>
        </div>
      </div>
      <div style={{ flexShrink: 0, ...labelStyle }}>
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

  const callService = useCallback(async (entity, service, optimisticState, serviceData) => {
    const domain = domainOf(entity.entity_id)
    setBusyIds((b) => ({ ...b, [entity.entity_id]: true }))
    if (optimisticState != null || serviceData?.temperature != null) {
      setEntities((list) =>
        list.map((e) =>
          e.entity_id === entity.entity_id
            ? {
                ...e,
                ...(optimisticState != null ? { state: optimisticState } : {}),
                ...(serviceData?.temperature != null
                  ? { attributes: { ...e.attributes, temperature: serviceData.temperature } }
                  : {}),
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
      await new Promise((r) => setTimeout(r, 1800))
      await load()
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
