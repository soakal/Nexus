import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'

// Persistent alert strip — polls /api/ha/entities every 30s for unavailable entities.
// Dismissible per entity_id, stored in localStorage until the alert changes.
export default function AlertStrip() {
  const [alert, setAlert] = useState(null)   // { entity_id, message }
  const [dismissed, setDismissed] = useState(() => {
    try { return JSON.parse(localStorage.getItem('nexus_dismissed_alerts') || '{}') } catch { return {} }
  })

  const poll = useCallback(async () => {
    try {
      const data = await api.ha.entities()
      const alerts = data.alerts || []
      if (alerts.length === 0) { setAlert(null); return }
      const first = alerts[0]
      // HA entity objects have entity_id and state; build a stable key
      const entityId = first.entity_id || first.id || String(first)
      const message = first.attributes?.friendly_name
        ? `${first.attributes.friendly_name} is ${first.state || 'unavailable'}`
        : `${entityId} is ${first.state || 'unavailable'}`
      setAlert({ entity_id: entityId, message })
    } catch {
      // Silently swallow — strip should not crash the app
    }
  }, [])

  useEffect(() => {
    poll()
    const id = setInterval(poll, 30000)
    return () => clearInterval(id)
  }, [poll])

  // Persist dismissed map
  useEffect(() => {
    localStorage.setItem('nexus_dismissed_alerts', JSON.stringify(dismissed))
  }, [dismissed])

  const dismiss = () => {
    if (!alert) return
    setDismissed((d) => ({ ...d, [alert.entity_id]: alert.message }))
  }

  // Show if there's an alert and it hasn't been dismissed with this same message
  const visible = alert && dismissed[alert.entity_id] !== alert.message

  if (!visible) return null

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: '10px',
      padding: '9px 16px',
      background: 'rgba(251,191,36,0.08)',
      borderBottom: '1px solid rgba(251,191,36,0.28)',
      flexShrink: 0,
    }}>
      {/* Amber dot */}
      <div style={{
        width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
        background: '#fbbf24', boxShadow: '0 0 6px rgba(251,191,36,0.6)',
      }} />

      <span style={{ flex: 1, fontSize: '13px', fontWeight: 600, color: '#f4d27a' }}>
        {alert.message}
      </span>

      <button
        onClick={dismiss}
        title="Dismiss"
        style={{
          background: 'none', border: 'none', cursor: 'pointer',
          color: '#8a96ad', fontSize: '16px', lineHeight: 1,
          padding: '0 2px', flexShrink: 0,
        }}
      >
        ×
      </button>
    </div>
  )
}
