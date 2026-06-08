import { useState } from 'react'
import { api } from '../lib/api'
export default function AdGuardToggle({ enabled: init, onChange }) {
  const [enabled, setEnabled] = useState(init)
  const [loading, setLoading] = useState(false)
  const toggle = async () => {
    setLoading(true)
    try {
      await api.adguard.toggle(!enabled)
      setEnabled(e => !e)
      onChange?.(!enabled)
    } catch {}
    setLoading(false)
  }
  return (
    <button onClick={toggle} disabled={loading}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${enabled ? 'bg-accent-green' : 'bg-border-dark'}`}>
      <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-6' : 'translate-x-1'}`} />
    </button>
  )
}
