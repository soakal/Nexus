export default function SourceCard({ name, healthy, metric, lastChecked, extra }) {
  return (
    <div
      className="hud-panel-sm p-3"
      style={{ borderColor: healthy ? 'rgba(0,212,255,0.2)' : 'rgba(255,45,45,0.3)' }}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="font-mono text-xs text-text-primary uppercase tracking-wider">{name}</span>
        <div className="flex items-center gap-1.5">
          <span className={healthy ? 'arc-dot' : 'arc-dot-err'} />
          <span className="hud-label">{healthy ? 'ONLINE' : 'OFFLINE'}</span>
        </div>
      </div>
      {metric && <div className="font-mono text-lg text-text-primary glow-cyan-text">{metric}</div>}
      {extra}
      {lastChecked && (
        <div className="text-text-secondary text-xs mt-2 font-mono">
          {new Date(lastChecked.endsWith('Z') ? lastChecked : lastChecked + 'Z').toLocaleTimeString()}
        </div>
      )}
    </div>
  )
}
