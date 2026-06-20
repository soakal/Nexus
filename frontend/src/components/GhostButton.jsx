export default function GhostButton({ children, onClick, disabled, icon, style = {} }) {
  return (
    <button onClick={onClick} disabled={disabled}
      style={{ display: 'flex', alignItems: 'center', gap: '7px', padding: '7px 13px', borderRadius: '9px',
        border: '1px solid rgba(120,160,220,0.16)', background: 'rgba(255,255,255,0.03)', color: '#cdd6e6',
        fontSize: '12px', fontWeight: 600, cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1, ...style }}>
      {icon}{children}
    </button>
  )
}
