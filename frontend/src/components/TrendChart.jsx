import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'

export default function TrendChart({ title, data, projection, unit, threshold }) {
  const hist = (data || []).map(d => ({ ...d, historical: d.value }))
  const lastHist = hist.length > 0 ? hist[hist.length - 1] : null

  // Prepend the last historical point to the projected series so the two lines
  // connect without a gap. The bridge point carries both keys so each Line
  // renders its final/first point at the same coordinate.
  const proj = (projection || []).map(d => ({ ...d, projected: d.value }))
  const bridged = lastHist
    ? [{ ...lastHist, projected: lastHist.value }, ...proj]
    : proj

  const combined = [...hist, ...bridged]

  return (
    <div className="hud-panel p-4">
      <h3 className="hud-label mb-4">{title}</h3>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={combined}>
          <CartesianGrid strokeDasharray="3 3" stroke="#0c2035" />
          <XAxis dataKey="timestamp" tick={{ fill: '#4d7c96', fontSize: 10 }} tickFormatter={v => v.slice(5, 10)} />
          <YAxis tick={{ fill: '#4d7c96', fontSize: 10 }} unit={unit} />
          <Tooltip contentStyle={{ background: '#080f1e', border: '1px solid rgba(0,212,255,0.25)', color: '#cce5f0', fontSize: 12 }} />
          <Line type="monotone" dataKey="historical" stroke="#00d4ff" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="projected" stroke="#ff9500" strokeWidth={2} strokeDasharray="5 5" dot={false} />
          {threshold && <ReferenceLine y={threshold} stroke="#ff2d2d" strokeDasharray="3 3" />}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
