import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'

export default function AdGuardToggle({ enabled: init, onChange }) {
  const [enabled, setEnabled] = useState(init)
  const [loading, setLoading] = useState(false)
  const [pausedUntil, setPausedUntil] = useState(null)
  const [minutesLeft, setMinutesLeft] = useState(0)
  const intervalRef = useRef(null)

  // Re-sync enabled from prop
  useEffect(() => setEnabled(init), [init])

  // Countdown interval when paused
  useEffect(() => {
    if (!pausedUntil) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
      return
    }

    const tick = () => {
      const remaining = Math.ceil((pausedUntil - Date.now()) / 60000)
      if (remaining <= 0) {
        setPausedUntil(null)
        setMinutesLeft(0)
      } else {
        setMinutesLeft(remaining)
      }
    }

    tick() // run immediately
    intervalRef.current = setInterval(tick, 10000)
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [pausedUntil])

  const toggle = async () => {
    setLoading(true)
    try {
      await api.adguard.toggle(!enabled)
      const next = !enabled
      setEnabled(next)
      onChange?.(next)
      // If re-enabling, clear any pause
      if (next) {
        setPausedUntil(null)
        setMinutesLeft(0)
      }
    } catch {}
    setLoading(false)
  }

  const timedDisable = async (minutes) => {
    setLoading(true)
    try {
      await api.adguard.timedDisable(minutes)
      setEnabled(false)
      onChange?.(false)
      setPausedUntil(Date.now() + minutes * 60 * 1000)
    } catch {}
    setLoading(false)
  }

  const isPaused = !!pausedUntil && minutesLeft > 0

  return (
    <div className="flex flex-col gap-2">
      {/* Main toggle button */}
      <button
        onClick={toggle}
        disabled={loading}
        className="hud-panel-sm p-2 flex items-center gap-2 disabled:opacity-50"
      >
        <span className={enabled ? 'arc-dot' : 'arc-dot-dim'} />
        <span className="hud-label">{enabled ? 'FILTERING ON' : 'FILTERING OFF'}</span>
      </button>

      {/* Timed disable controls — only show when filtering is enabled and not already paused */}
      {enabled && !isPaused && (
        <div className="flex items-center gap-1">
          {[
            { label: '15M', minutes: 15 },
            { label: '30M', minutes: 30 },
            { label: '1H',  minutes: 60 },
            { label: '3H',  minutes: 180 },
          ].map(({ label, minutes }) => (
            <button
              key={label}
              onClick={() => timedDisable(minutes)}
              disabled={loading}
              className="hud-label disabled:opacity-40"
              style={{
                border: '1px solid rgba(0,212,255,0.3)',
                padding: '2px 6px',
                fontSize: '0.65rem',
                cursor: loading ? 'not-allowed' : 'pointer',
                background: 'transparent',
                transition: 'border-color 0.15s, color 0.15s',
              }}
              onMouseEnter={e => {
                e.currentTarget.style.borderColor = 'rgba(0,212,255,0.8)'
                e.currentTarget.style.color = 'rgba(0,212,255,1)'
              }}
              onMouseLeave={e => {
                e.currentTarget.style.borderColor = 'rgba(0,212,255,0.3)'
                e.currentTarget.style.color = ''
              }}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {/* Paused indicator */}
      {isPaused && (
        <div className="flex items-center gap-2">
          <span className="arc-dot-warn" />
          <span className="hud-label text-accent-orange">
            PAUSED ({minutesLeft}m remaining)
          </span>
        </div>
      )}
    </div>
  )
}
