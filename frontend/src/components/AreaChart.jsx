export default function AreaChart({ data = [], color = '#2fd4ee', height = 150, gridLines = [40, 75, 110] }) {
  const vals = (data || []).map(d => (typeof d === 'number' ? d : d.value)).filter(v => v != null)
  if (vals.length < 2) return <div style={{ height, display: 'flex', alignItems: 'center', color: '#5d6982', fontSize: '12px' }}>Collecting data...</div>
  const w = 680, h = 150, p = 10
  const min = Math.min(...vals), max = Math.max(...vals), span = (max - min) || 1
  const pts = vals.map((v, i) => [p + i * (w - 2 * p) / (vals.length - 1), h - p - (v - min) / span * (h - 2 * p)])
  const line = pts.map(pt => pt[0].toFixed(1) + ',' + pt[1].toFixed(1)).join(' ')
  const area = line + ` ${w - p},${h - p} ${p},${h - p}`
  const gid = 'gc' + Math.abs(color.charCodeAt(1) || 0)
  return (
    <svg viewBox="0 0 680 150" preserveAspectRatio="none" style={{ width: '100%', height, display: 'block' }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {gridLines.map(y => <line key={y} x1="10" y1={y} x2="670" y2={y} stroke="rgba(120,160,220,0.08)" strokeWidth="1" />)}
      <polygon points={area} fill={`url(#${gid})`} />
      <polyline points={line} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  )
}
