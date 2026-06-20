export default function PrimaryButton({ children, onClick, disabled, icon, style = {} }) {
  return (
    <button onClick={onClick} disabled={disabled}
      style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 16px', borderRadius: '10px',
        border: '1px solid var(--ac-line)', background: 'var(--ac-dim)', color: 'var(--accent)',
        fontSize: '13px', fontWeight: 600, cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1, ...style }}>
      {icon}{children}
    </button>
  )
}
