import { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../lib/api'

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

function EntityRow({ entity, onToggle, busy }) {
  const domain = domainOf(entity.entity_id)
  const name = friendlyName(entity)
  const state = entity.state
  const on = isOn(state)
  const unavailable = state === 'unavailable' || state === 'unknown'

  if (TOGGLE_DOMAINS.has(domain)) {
    return (
      <div className="flex items-center justify-between hud-panel px-3 py-2">
        <div className="min-w-0 flex items-center gap-2">
          <span className={unavailable ? 'arc-dot-dim' : on ? 'arc-dot' : 'arc-dot-dim'} />
          <div className="min-w-0">
            <div className="text-text-primary text-sm truncate">{name}</div>
            <div className="text-text-secondary text-xs font-mono truncate">{entity.entity_id}</div>
          </div>
        </div>
        <button
          disabled={busy || unavailable}
          onClick={() => onToggle(entity, on)}
          className={`ml-3 shrink-0 px-3 py-1 rounded-none text-xs font-mono font-bold uppercase tracking-widest transition-colors ${
            unavailable
              ? 'bg-bg-secondary text-text-secondary border border-border-dark cursor-not-allowed'
              : on
              ? 'bg-accent-green/15 text-accent-green border border-accent-green/50 hover:bg-accent-green/25'
              : 'bg-white/5 text-text-secondary border border-border-dark hover:text-text-primary'
          } ${busy ? 'opacity-50 cursor-wait' : ''}`}
        >
          {unavailable ? 'N/A' : on ? 'On' : 'Off'}
        </button>
      </div>
    )
  }

  // Sensors and everything else: read-only state display
  const unit = entity.attributes?.unit_of_measurement || ''
  return (
    <div className="flex items-center justify-between hud-panel px-3 py-2">
      <div className="min-w-0 flex items-center gap-2">
        <span className={unavailable ? 'arc-dot-err' : 'arc-dot-dim'} />
        <div className="min-w-0">
          <div className="text-text-primary text-sm truncate">{name}</div>
          <div className="text-text-secondary text-xs font-mono truncate">{entity.entity_id}</div>
        </div>
      </div>
      <div
        className={`ml-3 shrink-0 text-sm font-mono ${
          unavailable ? 'text-accent-orange' : 'text-accent-cyan glow-cyan-text'
        }`}
      >
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
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
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
    <div className="p-6 max-w-3xl">
      <div className="flex items-baseline justify-between mb-4">
        <h1 className="page-header">HOME SYSTEMS</h1>
        <span className="text-text-secondary text-xs font-mono">
          {entities.length} entities
          {cloudAlerts.length > 0 && <span className="arc-dot-warn ml-2 inline-block" />}
          {alerts.length > 0 && (
            <span className="text-accent-orange ml-2">{alerts.length} unavailable</span>
          )}
        </span>
      </div>

      {cloudAlerts.length > 0 && (
        <div className="hud-panel mb-6 px-3 py-3" style={{ borderColor: 'rgba(255,149,0,0.4)' }}>
          <div className="flex items-center gap-2 mb-2">
            <span className="arc-dot-warn" />
            <span className="hud-label text-accent-orange">HA CLOUD ALERT</span>
          </div>
          <div className="space-y-2">
            {cloudAlerts.map((ca) => (
              <div key={ca.entity} className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-text-primary text-sm">{ca.message}</div>
                  <div className="text-text-secondary text-xs font-mono truncate">
                    {ca.entity} · <span className="text-accent-orange">{ca.state}</span>
                  </div>
                </div>
                <button
                  disabled={reloading}
                  onClick={reloadCloud}
                  className={`glow-btn-gold shrink-0 ${reloading ? 'opacity-50 cursor-wait' : ''}`}
                >
                  {reloading ? 'RELOADING...' : 'RELOAD CLOUD'}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <input
        type="text"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter by name or entity id..."
        className="hud-input w-full mb-6"
      />

      {error && (
        <div className="mb-4 hud-panel border-accent-orange/40 px-3 py-2 flex items-center gap-2">
          <span className="arc-dot-err" />
          <span className="text-accent-orange text-sm">{error}</span>
        </div>
      )}

      {loading ? (
        <div className="hud-label animate-pulse">LOADING...</div>
      ) : domains.length === 0 ? (
        <div className="text-text-secondary text-sm">No entities match.</div>
      ) : (
        <div className="space-y-6">
          {domains.map((d) => (
            <div key={d}>
              <h2 className="hud-label mb-2">
                {d} <span className="text-text-secondary/60">({grouped[d].length})</span>
              </h2>
              <div className="space-y-2">
                {grouped[d].map((e) => (
                  <EntityRow
                    key={e.entity_id}
                    entity={e}
                    onToggle={toggle}
                    busy={!!busyIds[e.entity_id]}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
