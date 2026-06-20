export default function StatChip({ label, value, valueColor = '#e9eef8' }) {
  return (
    <div>
      <div style={{ fontSize: '11px', color: '#5d6982' }}>{label}</div>
      <div style={{ fontSize: '16px', fontWeight: 600, marginTop: '3px', color: valueColor }}>{value}</div>
    </div>
  )
}
