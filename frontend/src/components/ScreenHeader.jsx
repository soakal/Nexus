import Eyebrow from './Eyebrow'

export default function ScreenHeader({ section, title, subline, right }) {
  return (
    <header style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px' }}>
      <div>
        <div style={{ fontSize: '11px', letterSpacing: '0.16em', color: '#5d6982', fontWeight: 600, textTransform: 'uppercase', marginBottom: '7px' }}>Nexus · {section}</div>
        <h1 style={{ margin: 0, fontSize: 'clamp(22px,3vw,27px)', fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</h1>
        {subline && <div style={{ fontSize: '13px', color: '#8a96ad', marginTop: '6px' }}>{subline}</div>}
      </div>
      {right}
    </header>
  )
}
