import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../lib/api'

// Static command list
const PAGE_CMDS = [
  { id: 'nav-dashboard',  label: 'Go to Dashboard',      to: '/' },
  { id: 'nav-briefing',   label: 'Go to Briefing',        to: '/briefing' },
  { id: 'nav-today',      label: 'Go to Today',           to: '/today' },
  { id: 'nav-tasks',      label: 'Go to Tasks',           to: '/tasks' },
  { id: 'nav-chat',       label: 'Go to Chat',            to: '/chat' },
  { id: 'nav-media',      label: 'Go to Media',           to: '/media' },
  { id: 'nav-ha',         label: 'Go to Home Assistant',  to: '/ha' },
  { id: 'nav-uptime',     label: 'Go to Uptime',          to: '/uptime' },
  { id: 'nav-agents',     label: 'Go to Agents',          to: '/agents' },
  { id: 'nav-safety',     label: 'Go to Safety',          to: '/safety' },
  { id: 'nav-facts',      label: 'Go to Facts',           to: '/facts' },
  { id: 'nav-settings',   label: 'Go to Settings',        to: '/settings' },
]

const SLASH_CMDS = [
  { id: 'cmd-status',   label: '/status',         hint: 'Check system status' },
  { id: 'cmd-remember', label: '/remember [text]', hint: 'Remember something in the vault' },
]

async function runChatCmd(message) {
  const key = localStorage.getItem('nexus_api_key') || ''
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}` },
    body: JSON.stringify({ message }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let text = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    text += dec.decode(value, { stream: true })
  }
  return text
}

export default function CommandPalette({ open, onClose }) {
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const inputRef = useRef(null)
  const navigate = useNavigate()

  // Reset on open
  useEffect(() => {
    if (open) {
      setQuery('')
      setSelected(0)
      setResult(null)
      setRunning(false)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  const filtered = useCallback(() => {
    const q = query.trim().toLowerCase()
    const all = [...SLASH_CMDS, ...PAGE_CMDS]
    if (!q) return all
    return all.filter(c => c.label.toLowerCase().includes(q) || (c.hint || '').toLowerCase().includes(q))
  }, [query])

  const items = filtered()

  const run = useCallback(async (item) => {
    if (!item) return

    // Navigation commands
    if (item.to) {
      navigate(item.to)
      onClose()
      return
    }

    // /status
    if (item.id === 'cmd-status') {
      setRunning(true)
      setResult(null)
      try {
        const text = await runChatCmd('status')
        setResult({ ok: true, text: text.slice(0, 400) })
      } catch (e) {
        setResult({ ok: false, text: e.message })
      } finally {
        setRunning(false)
      }
      return
    }

    // /remember [text] — extract text after "/remember "
    if (item.id === 'cmd-remember') {
      const text = query.replace(/^\/remember\s*/i, '').trim()
      if (!text) { inputRef.current?.focus(); return }
      setRunning(true)
      setResult(null)
      try {
        await runChatCmd(`remember ${text}`)
        setResult({ ok: true, text: 'Saved to vault.' })
      } catch (e) {
        setResult({ ok: false, text: e.message })
      } finally {
        setRunning(false)
      }
    }
  }, [query, navigate, onClose])

  // Keyboard navigation
  useEffect(() => {
    if (!open) return
    const onKey = (e) => {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'ArrowDown') { e.preventDefault(); setSelected((s) => Math.min(s + 1, items.length - 1)) }
      if (e.key === 'ArrowUp') { e.preventDefault(); setSelected((s) => Math.max(s - 1, 0)) }
      if (e.key === 'Enter') { e.preventDefault(); run(items[selected]) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, items, selected, run, onClose])

  if (!open) return null

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.65)',
          zIndex: 200, backdropFilter: 'blur(4px)',
        }}
      />

      {/* Palette panel */}
      <div style={{
        position: 'fixed', top: '18vh', left: '50%', transform: 'translateX(-50%)',
        width: 'min(560px, 92vw)', zIndex: 201,
        background: 'linear-gradient(180deg,rgba(255,255,255,0.04),rgba(255,255,255,0)),#0d1525',
        border: '1px solid rgba(47,212,238,0.22)',
        borderRadius: '16px',
        boxShadow: '0 24px 60px rgba(0,0,0,0.7), 0 0 0 1px rgba(47,212,238,0.06)',
        overflow: 'hidden',
      }}>
        {/* Input */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: '10px',
          padding: '14px 16px',
          borderBottom: '1px solid rgba(120,160,220,0.10)',
        }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8a96ad" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(0); setResult(null) }}
            placeholder="Search or type /status, /remember …"
            style={{
              flex: 1, background: 'none', border: 'none', outline: 'none',
              fontSize: '15px', color: '#e9eef8',
              fontFamily: "'Space Grotesk', system-ui, sans-serif",
            }}
          />
          <span style={{ fontSize: '11px', color: '#465069', fontWeight: 600 }}>ESC</span>
        </div>

        {/* Results */}
        <div style={{ maxHeight: '340px', overflowY: 'auto' }}>
          {running && (
            <div style={{ padding: '16px', fontSize: '13px', color: '#8a96ad' }}>Running…</div>
          )}
          {result && !running && (
            <div style={{
              padding: '12px 16px', fontSize: '13px',
              color: result.ok ? '#34d399' : '#fb7185',
              borderBottom: '1px solid rgba(120,160,220,0.08)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              {result.text || (result.ok ? 'Done.' : 'Error')}
            </div>
          )}
          {!running && items.length === 0 && (
            <div style={{ padding: '16px', fontSize: '13px', color: '#8a96ad' }}>No commands match.</div>
          )}
          {!running && items.map((item, i) => (
            <div
              key={item.id}
              onClick={() => run(item)}
              onMouseEnter={() => setSelected(i)}
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                gap: '10px', padding: '10px 16px', cursor: 'pointer',
                background: i === selected ? 'rgba(47,212,238,0.08)' : 'transparent',
                borderLeft: i === selected ? '2px solid var(--accent)' : '2px solid transparent',
              }}
            >
              <div>
                <div style={{ fontSize: '14px', fontWeight: 600, color: i === selected ? 'var(--accent)' : '#dbe3f0' }}>
                  {item.label}
                </div>
                {item.hint && (
                  <div style={{ fontSize: '11px', color: '#5d6982', marginTop: '1px' }}>{item.hint}</div>
                )}
              </div>
              {item.to && (
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#465069" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12h14M12 5l7 7-7 7"/>
                </svg>
              )}
            </div>
          ))}
        </div>

        {/* Footer hint */}
        <div style={{
          padding: '8px 16px', borderTop: '1px solid rgba(120,160,220,0.08)',
          display: 'flex', gap: '16px',
        }}>
          {[['↑↓', 'navigate'], ['↵', 'run'], ['Esc', 'close']].map(([key, label]) => (
            <span key={key} style={{ fontSize: '11px', color: '#465069' }}>
              <span style={{ color: '#8a96ad', fontWeight: 700 }}>{key}</span> {label}
            </span>
          ))}
        </div>
      </div>
    </>
  )
}
