import { useState } from 'react'
import { api } from '../lib/api'

export default function BrainOrganizerCard({ data, onRun }) {
  const [triggering, setTriggering] = useState(false)
  const [triggerError, setTriggerError] = useState(null)

  const isRunning = data?.running || triggering

  const handleRun = async () => {
    setTriggerError(null)
    setTriggering(true)
    try {
      await api.brain.run()
      onRun?.()
    } catch (e) {
      setTriggerError(e.message?.includes('409') ? 'ALREADY RUNNING' : 'TRIGGER FAILED')
    } finally {
      setTriggering(false)
    }
  }

  const lastRun = data?.last_run
    ? new Date(data.last_run).toLocaleString()
    : 'Never'

  const succeeded = data?.succeeded ?? 0
  const pending = data?.pending ?? 0
  const failed = data?.failed ?? 0

  return (
    <div className="hud-panel p-4">
      <div className="flex items-center justify-between">
        <span className="hud-label border-l-2 border-accent-cyan pl-2">BRAIN ORGANIZER</span>
        <div className="flex items-center gap-2">
          {isRunning && (
            <span className="text-accent-cyan text-xs font-mono animate-pulse">● RUNNING</span>
          )}
          <button
            onClick={handleRun}
            disabled={isRunning}
            className="glow-btn px-3 py-1 text-xs disabled:opacity-40"
          >
            {triggering ? 'STARTING...' : 'RUN NOW'}
          </button>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-3 text-center">
        <div>
          <div className="font-mono text-xl text-accent-green">{succeeded}</div>
          <div className="hud-label text-xs mt-1">PROCESSED</div>
        </div>
        <div>
          <div className={`font-mono text-xl ${pending > 0 ? 'text-accent-orange' : 'text-text-secondary'}`}>
            {pending}
          </div>
          <div className="hud-label text-xs mt-1">PENDING</div>
        </div>
        <div>
          <div className={`font-mono text-xl ${failed > 0 ? 'text-accent-orange' : 'text-text-secondary'}`}>
            {failed}
          </div>
          <div className="hud-label text-xs mt-1">FAILED</div>
        </div>
      </div>

      <div className="mt-2 text-text-secondary text-xs font-mono">
        Last run: {lastRun}
      </div>
      <div className="text-text-secondary text-xs font-mono">
        Scheduled: daily 2:00 AM
      </div>

      {triggerError && (
        <div className="mt-1 text-accent-orange text-xs font-mono">{triggerError}</div>
      )}

      {data?.log_tail?.length > 0 && (
        <div className="mt-3 border-t border-border-dark pt-2 space-y-1">
          {data.log_tail.map((line, i) => (
            <div
              key={i}
              className={`text-xs font-mono truncate ${
                line.includes('[ERROR]') ? 'text-accent-orange' :
                line.includes('[WARNING]') ? 'text-accent-orange opacity-70' :
                'text-text-secondary'
              }`}
            >
              {line.replace(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} /, '')}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
