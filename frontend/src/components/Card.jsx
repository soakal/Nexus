export default function Card({ children, style = {}, flex, accent, dashed = false }) {
  const base = {
    background: accent === 'amber'
      ? 'linear-gradient(180deg,rgba(232,196,104,0.05),rgba(255,255,255,0)),#1c1c1c'
      : accent === 'cyan'
      ? 'linear-gradient(180deg,rgba(255,138,61,0.05),rgba(255,255,255,0)),#1c1c1c'
      : 'linear-gradient(180deg,rgba(255,255,255,0.025),rgba(255,255,255,0)),#1c1c1c',
    border: dashed ? '1px dashed rgba(180,178,170,0.16)'
      : accent === 'amber' ? '1px solid rgba(232,196,104,0.25)'
      : accent === 'cyan' ? '1px solid var(--ac-line)'
      : '1px solid rgba(180,178,170,0.10)',
    borderRadius: '6px',
    padding: 'var(--pad)',
  }
  if (flex) base.flex = flex
  return <div style={{ ...base, ...style }}>{children}</div>
}
