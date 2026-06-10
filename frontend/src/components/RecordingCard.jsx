import { parseUTC } from '../lib/parseUTC'

function formatTime(iso) {
  if (!iso) return null
  const d = parseUTC(iso)
  if (isNaN(d.getTime())) return null
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

export default function RecordingCard({ recording }) {
  const startTime = formatTime(recording.start)

  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="arc-dot-rec flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-text-primary text-sm truncate block leading-tight">{recording.title}</span>
        <div className="flex items-center gap-2 mt-0.5">
          {recording.channel && (
            <span className="inline-block bg-bg-secondary border border-accent-cyan text-accent-cyan font-mono text-xs px-1.5 py-0.5 leading-none tracking-wider flex-shrink-0">
              CH {recording.channel}
            </span>
          )}
          {startTime && (
            <span className="hud-label text-xs">{startTime}</span>
          )}
        </div>
      </div>
    </div>
  )
}
