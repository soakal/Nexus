import { useState } from 'react'
import Eyebrow from './Eyebrow'

export default function ScreenHeader({ section, title, subline, help, right }) {
  const [showHelp, setShowHelp] = useState(false)
  return (
    <header style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px' }}>
      <div>
        <div style={{ fontSize: '11px', letterSpacing: '0.16em', color: '#7a776d', fontWeight: 600, textTransform: 'uppercase', marginBottom: '7px' }}>Nexus · {section}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '9px' }}>
          <h1 style={{ margin: 0, fontSize: 'clamp(22px,3vw,27px)', fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</h1>
          {help && (
            <button
              onClick={() => setShowHelp(v => !v)}
              aria-label="What this page shows"
              aria-expanded={showHelp}
              style={{
                width: '20px', height: '20px', borderRadius: '50%', flexShrink: 0,
                border: '1px solid rgba(180,178,170,0.28)',
                background: showHelp ? 'rgba(47,212,238,0.16)' : 'rgba(255,255,255,0.04)',
                color: showHelp ? 'var(--accent, #2fd4ee)' : '#98958c',
                fontSize: '11px', fontWeight: 700, lineHeight: 1, cursor: 'pointer',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              }}
            >?</button>
          )}
        </div>
        {subline && <div style={{ fontSize: '13px', color: '#98958c', marginTop: '6px' }}>{subline}</div>}
        {help && showHelp && (
          <div style={{
            fontSize: '13px', lineHeight: 1.5, color: '#c2bfb5', marginTop: '10px',
            maxWidth: '640px', padding: '10px 13px', borderRadius: '6px',
            border: '1px solid rgba(180,178,170,0.16)', background: 'rgba(255,255,255,0.03)',
          }}>{help}</div>
        )}
      </div>
      {right}
    </header>
  )
}
