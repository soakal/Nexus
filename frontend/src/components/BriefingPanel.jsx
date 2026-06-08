import { useState } from 'react'
export default function BriefingPanel({ content }) {
  if (!content) return <div className="text-text-secondary text-sm">No briefing yet.</div>
  const sections = content.split(/^## /m).filter(Boolean)
  return (
    <div className="space-y-4">
      {sections.map((section, i) => {
        const [title, ...body] = section.split('\n')
        return (
          <div key={i} className="bg-bg-card border border-border-dark rounded-lg p-4">
            <h3 className="text-accent-cyan font-mono text-sm font-bold mb-2">{title}</h3>
            <pre className="text-text-primary text-sm whitespace-pre-wrap font-sans">{body.join('\n').trim()}</pre>
          </div>
        )
      })}
    </div>
  )
}
