import { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'
import GhostButton from '../components/GhostButton'
import TextInput from '../components/TextInput'

const TOGGLE_DOMAINS = new Set(['light', 'switch', 'fan', 'input_boolean'])
const ON_STATES = new Set(['on', 'open', 'home', 'playing', 'active', 'unlocked'])

function domainOf(entityId) {
  return entityId.split('.')[0]
}

function friendlyName(entity) {
  return entity.attributes?.friendly_name || entity.entity_id
}

function isOn(state) {
  return ON_STATES.has((state || '').toLowerCase())
}

function entityDotColor(state) {
  if (state === 'unavailable' || state === 'unknown') return '#fbbf24'
  if (isOn(state) || state === 'armed') return '#34d399'
  return '#7c8aa3'
}

function EntityRow({ entity, onToggle, busy }) {
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
            <div style={idStyle}>{entity.entity_id}</div>
          </div>
        </div>
        <button
          disabled={busy || unavailable}
          onClick={() => onToggle(entity, on)}
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

  // Sensors and everything else: read-only state display
  const unit = entity.attributes?.unit_of_measurement || ''
  return (
    <div style={rowStyle}>
      <div style={leftStyle}>
        <StatusDot color={dotColor} size={8} glow={false} />
        <div style={{ minWidth: 0 }}>
          <div style={nameStyle}>{name}</div>
          <div style={idStyle}>{entity.entity_id}</div>
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
      setEntities(data.entities || [])
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

  const toggle = useCallback(async (entity, currentlyOn) => {
    const domain = domainOf(entity.entity_id)
    const service = currentlyOn ? 'turn_off' : 'turn_on'
    setBusyIds((b) => ({ ...b, [entity.entity_id]: true }))
    // Optimistic update
    setEntities((list) =>
      list.map((e) =>
        e.entity_id === entity.entity_id ? { ...e, state: currentlyOn ? 'off' : 'on' } : e
      )
    )
    try {
      await api.ha.service(domain, service, entity.entity_id)
      await load()
    } catch (e) {
      setError(String(e.message || e))
      await load() // resync truth on failure
    } finally {
      setBusyIds((b) => {
        const next = { ...b }
        delete next[entity.entity_id]
        return next
      })
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
      const d = domainOf(e.entity_id)
      if (!groups[d]) groups[d] = []
      groups[d].push(e)
    }
    for (const d of Object.keys(groups)) {
      groups[d].sort((a, b) => friendlyName(a).localeCompare(friendlyName(b)))
    }
    return groups
  }, [entities, filter])

  const domains = useMemo(() => Object.keys(grouped).sort(), [grouped])

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
                  onToggle={toggle}
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
