export default function TextInput({ style = {}, ...props }) {
  return (
    <input {...props}
      style={{ padding: '12px 14px', borderRadius: '6px', border: '1px solid rgba(180,178,170,0.16)',
        background: 'rgba(255,255,255,0.03)', color: '#f4f3f0', fontSize: '14px', outline: 'none', ...style }} />
  )
}
