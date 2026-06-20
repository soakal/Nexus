export default function TextInput({ style = {}, ...props }) {
  return (
    <input {...props}
      style={{ padding: '12px 14px', borderRadius: '11px', border: '1px solid rgba(120,160,220,0.16)',
        background: 'rgba(255,255,255,0.03)', color: '#e9eef8', fontSize: '14px', outline: 'none', ...style }} />
  )
}
