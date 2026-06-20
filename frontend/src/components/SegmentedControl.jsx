export default function SegmentedControl({ options, value, onChange }) {
  const opts = options.map(o => typeof o === 'string' ? { value: o, label: o } : o)
  const seg = (active) => ({
    padding: '5px 12px', fontSize: '11px', fontWeight: 600, letterSpacing: '0.04em',
    borderRadius: '7px', cursor: 'pointer', transition: 'all .15s',
    border: active ? '1px solid var(--ac-line)' : '1px solid transparent',
    background: active ? 'var(--ac-dim)' : 'transparent',
    color: active ? 'var(--accent)' : '#5d6982',
  })
  return (
    <div style={{ display: 'flex', gap: '5px', background: 'rgba(120,160,220,0.07)', padding: '4px', borderRadius: '10px' }}>
      {opts.map(o => (
        <button key={o.value} onClick={() => onChange(o.value)} style={seg(value === o.value)}>{o.label}</button>
      ))}
    </div>
  )
}
