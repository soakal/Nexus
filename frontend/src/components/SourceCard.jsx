import { useState } from 'react'
export default function SourceCard({ name, healthy, metric, lastChecked, extra }) {
  return (
    <div className="bg-bg-card border border-border-dark rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-text-secondary text-xs uppercase tracking-wider">{name}</span>
        <span className={`w-2 h-2 rounded-full ${healthy ? 'bg-accent-green' : 'bg-accent-orange'} ${healthy ? 'animate-pulse' : ''}`} />
      </div>
      {metric && <div className="font-mono text-lg text-text-primary">{metric}</div>}
      {extra}
      {lastChecked && <div className="text-text-secondary text-xs mt-2">{new Date(lastChecked.endsWith('Z') ? lastChecked : lastChecked + 'Z').toLocaleTimeString()}</div>}
    </div>
  )
}
