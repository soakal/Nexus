import StatusDot from './StatusDot'

const TONES = {
  green:  { dot: '#34d399', text: '#5fe0b4', bg: 'rgba(52,211,153,0.10)',   bd: 'rgba(52,211,153,0.22)' },
  amber:  { dot: '#fbbf24', text: '#f4d27a', bg: 'rgba(251,191,36,0.10)',   bd: 'rgba(251,191,36,0.25)' },
  red:    { dot: '#fb7185', text: '#fb7185', bg: 'rgba(251,113,133,0.10)',  bd: 'rgba(251,113,133,0.30)' },
  grey:   { dot: '#7c8aa3', text: '#9aa6bd', bg: 'rgba(120,160,220,0.08)',  bd: 'rgba(120,160,220,0.14)' },
  accent: { dot: 'var(--accent)', text: 'var(--accent)', bg: 'var(--ac-dim)', bd: 'var(--ac-line)' },
}

export default function StatusPill({ label, tone = 'green', dot = true, dotPulse = false, dotRing = false, style = {} }) {
  const t = TONES[tone] || TONES.green
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: '7px', padding: '5px 11px', borderRadius: '20px', background: t.bg, border: `1px solid ${t.bd}`, ...style }}>
      {dot && <StatusDot color={t.dot} size={6} glow={false} pulse={dotPulse} ring={dotRing} />}
      <span style={{ fontSize: '11px', color: t.text, fontWeight: 600 }}>{label}</span>
    </span>
  )
}
