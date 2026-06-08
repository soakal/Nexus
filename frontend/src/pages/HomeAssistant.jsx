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
      <div className="flex items-center justify-between bg-bg-card border border-border-dark rounded px-3 py-2">
        <div className="min-w-0">
          <div className="text-text-primary text-sm truncate">{name}</div>
          <div className="text-text-secondary text-xs font-mono truncate">{entity.entity_id}</div>
        </div>
        <button
          disabled={busy || unavailable}
          onClick={() => onToggle(entity, on)}
          className={`ml-3 shrink-0 px-3 py-1 rounded text-xs font-mono font-bold uppercase tracking-wider transition-colors ${
            unavailable
              ? 'bg-border-dark text-text-secondary cursor-not-allowed'
              : on
              ? 'bg-accent-green/20 text-accent-green border border-accent-green/50 hover:bg-accent-green/30'
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
    <div className="flex items-center justify-between bg-bg-card border border-border-dark rounded px-3 py-2">
      <div className="min-w-0">
        <div className="text-text-primary text-sm truncate">{name}</div>
        <div className="text-text-secondary text-xs font-mono truncate">{entity.entity_id}</div>
      </div>
      <div
        className={`ml-3 shrink-0 text-sm font-mono ${
          unavailable ? 'text-accent-orange' : 'text-accent-cyan'
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
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [busyIds, setBusyIds] = useState({})

  const load = useCallback(async () => {
    try {
      const data = await api.ha.entities()
      setEntities(data.entities || [])
      setAlerts(data.alerts || [])
      setError(null)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }, [])

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
        <h1 className="font-mono text-accent-cyan text-xl font-bold">HOME ASSISTANT</h1>
        <span className="text-text-secondary text-xs font-mono">
          {entities.length} entities
          {alerts.length > 0 && (
            <span className="text-accent-orange ml-2">{alerts.length} unavailable</span>
          )}
        </span>
      </div>

      <input
        type="text"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter by name or entity id..."
        className="w-full mb-6 bg-bg-card border border-border-dark rounded px-3 py-2 text-sm text-text-primary placeholder:text-text-secondary font-mono focus:outline-none focus:border-accent-cyan"
      />

      {error && (
        <div className="mb-4 bg-accent-orange/10 border border-accent-orange/40 rounded px-3 py-2 text-accent-orange text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-text-secondary">Loading...</div>
      ) : domains.length === 0 ? (
        <div className="text-text-secondary text-sm">No entities match.</div>
      ) : (
        <div className="space-y-6">
          {domains.map((d) => (
            <div key={d}>
              <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-2">
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
