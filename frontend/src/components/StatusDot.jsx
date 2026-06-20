export default function StatusDot({ color = '#34d399', size = 7, glow = true, pulse = false, ring = false, style = {} }) {
  const s = {
    width: size, height: size, borderRadius: '50%', background: color, flex: 'none',
    ...(glow ? { boxShadow: `0 0 ${size + 1}px ${color}` } : {}),
    ...(pulse ? { animation: 'nx-pulse 2.4s infinite' } : {}),
    ...(ring ? { animation: 'nx-ring 2.2s infinite' } : {}),
    ...style,
  }
  return <span style={s} />
}
