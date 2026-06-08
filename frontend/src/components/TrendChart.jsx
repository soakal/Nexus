import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
export default function TrendChart({ title, data, projection, unit, threshold }) {
  const combined = [
    ...(data || []).map(d => ({ ...d, historical: d.value })),
    ...(projection || []).map(d => ({ ...d, projected: d.value })),
  ]
  return (
    <div className="bg-bg-card border border-border-dark rounded-lg p-4">
      <h3 className="text-text-secondary text-xs uppercase tracking-wider mb-4">{title}</h3>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={combined}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e2d4a" />
          <XAxis dataKey="timestamp" tick={{ fill: '#7a8aaa', fontSize: 10 }} tickFormatter={v => v.slice(5, 10)} />
          <YAxis tick={{ fill: '#7a8aaa', fontSize: 10 }} unit={unit} />
          <Tooltip contentStyle={{ background: '#141d35', border: '1px solid #1e2d4a', color: '#e8edf8', fontSize: 12 }} />
          <Line type="monotone" dataKey="historical" stroke="#00d4ff" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="projected" stroke="#ff6b2b" strokeWidth={2} strokeDasharray="5 5" dot={false} />
          {threshold && <ReferenceLine y={threshold} stroke="#ff6b2b" strokeDasharray="3 3" />}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
