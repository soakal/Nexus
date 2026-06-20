export default function Eyebrow({ children, style = {} }) {
  return <span style={{ fontSize: '11px', letterSpacing: '0.14em', textTransform: 'uppercase', color: '#5d6982', fontWeight: 600, ...style }}>{children}</span>
}
