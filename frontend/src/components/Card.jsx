export default function Card({ children, style = {}, flex, accent, dashed = false }) {
  const base = {
    background: accent === 'amber'
      ? 'linear-gradient(180deg,rgba(251,191,36,0.05),rgba(255,255,255,0)),#0c1320'
      : accent === 'cyan'
      ? 'linear-gradient(180deg,rgba(47,212,238,0.05),rgba(255,255,255,0)),#0c1320'
      : 'linear-gradient(180deg,rgba(255,255,255,0.025),rgba(255,255,255,0)),#0c1320',
    border: dashed ? '1px dashed rgba(120,160,220,0.16)'
      : accent === 'amber' ? '1px solid rgba(251,191,36,0.25)'
      : accent === 'cyan' ? '1px solid var(--ac-line)'
      : '1px solid rgba(120,160,220,0.10)',
    borderRadius: '16px',
    padding: 'var(--pad)',
  }
  if (flex) base.flex = flex
  return <div style={{ ...base, ...style }}>{children}</div>
}
