import { useState, useEffect, useRef } from 'react'
import { Mic } from 'lucide-react'
import { api } from '../lib/api'
import ScreenHeader from '../components/ScreenHeader'
import GhostButton from '../components/GhostButton'
import PrimaryButton from '../components/PrimaryButton'

// Inline markdown renderer reused from BriefingPanel style
function renderInline(text) {
  const boldParts = text.split(/\*\*(.+?)\*\*/g)
  const result = []
  boldParts.forEach((part, idx) => {
    if (idx % 2 === 1) {
      result.push(<strong key={`b${idx}`}>{part}</strong>)
    } else {
      const italicParts = part.split(/\*(.+?)\*/g)
      italicParts.forEach((iPart, iIdx) => {
        if (iIdx % 2 === 1) {
          result.push(<em key={`i${idx}-${iIdx}`}>{iPart}</em>)
        } else if (iPart) {
          result.push(iPart)
        }
      })
    }
  })
  return result
}

function renderMessageContent(content) {
  const lines = content.split('\n')
  const elements = []
  let listItems = []
  let listType = null

  function flushList(key) {
    if (!listItems.length) return
    if (listType === 'ul') {
      elements.push(
        <ul key={`list-${key}`} style={{ listStyleType: 'disc', paddingLeft: '20px', fontSize: '14px', lineHeight: '1.6', margin: '4px 0' }}>
          {listItems.map((item, i) => <li key={i} style={{ marginBottom: '2px' }}>{renderInline(item)}</li>)}
        </ul>
      )
    } else if (listType === 'ol') {
      elements.push(
        <ol key={`list-${key}`} style={{ listStyleType: 'decimal', paddingLeft: '20px', fontSize: '14px', lineHeight: '1.6', margin: '4px 0' }}>
          {listItems.map((item, i) => <li key={i} style={{ marginBottom: '2px' }}>{renderInline(item)}</li>)}
        </ol>
      )
    }
    listItems = []
    listType = null
  }

  lines.forEach((line, idx) => {
    const bulletMatch = line.match(/^[-*]\s+(.*)/)
    const numberedMatch = line.match(/^\d+\.\s+(.*)/)
    const h2Match = line.match(/^## (.+)/)
    const h3Match = line.match(/^### (.+)/)

    if (h2Match) {
      flushList(idx)
      elements.push(
        <p key={`h2-${idx}`} style={{ color: 'var(--accent)', fontSize: '11px', letterSpacing: '0.12em', fontWeight: 700, textTransform: 'uppercase', marginTop: '12px', marginBottom: '4px' }}>{h2Match[1]}</p>
      )
    } else if (h3Match) {
      flushList(idx)
      elements.push(
        <p key={`h3-${idx}`} style={{ color: '#e9eef8', fontSize: '12px', fontWeight: 600, letterSpacing: '0.06em', textTransform: 'uppercase', marginTop: '8px', marginBottom: '2px' }}>{h3Match[1]}</p>
      )
    } else if (bulletMatch) {
      if (listType === 'ol') flushList(idx)
      listType = 'ul'
      listItems.push(bulletMatch[1])
    } else if (numberedMatch) {
      if (listType === 'ul') flushList(idx)
      listType = 'ol'
      listItems.push(numberedMatch[1])
    } else if (line.trim() === '') {
      flushList(idx)
    } else {
      flushList(idx)
      elements.push(
        <p key={`p-${idx}`} style={{ fontSize: '14px', lineHeight: '1.6', margin: '2px 0' }}>{renderInline(line)}</p>
      )
    }
  })
  flushList('end')
  return elements
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  try {
    return new Date(isoStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}

const hasMediaRecorder = typeof window !== 'undefined' && !!window.MediaRecorder

const PlusIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" y1="5" x2="12" y2="19" />
    <line x1="5" y1="12" x2="19" y2="12" />
  </svg>
)

export default function Chat() {
  const [messages, setMessages] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [conversations, setConversations] = useState([])
  const [loadingConv, setLoadingConv] = useState(false)
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const mediaRecorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)

  const refreshConversations = async () => {
    try {
      const list = await api.chat.conversations()
      setConversations(list || [])
    } catch {
      // non-fatal
    }
  }

  useEffect(() => {
    refreshConversations()
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, sending])

  // Cleanup mic stream on unmount
  useEffect(() => {
    return () => {
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
        mediaRecorderRef.current.stop()
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(t => t.stop())
        streamRef.current = null
      }
    }
  }, [])

  const startNewChat = () => {
    setConversationId(null)
    setMessages([])
    inputRef.current?.focus()
  }

  const deleteCurrentConversation = async () => {
    if (!conversationId) return
    if (!window.confirm('Delete this conversation? This cannot be undone.')) return
    try {
      await api.chat.delete(conversationId)
      startNewChat()
      await refreshConversations()
    } catch {
      // non-fatal -- picker just won't reflect the deletion
    }
  }

  const loadConversation = async (id) => {
    if (!id) return
    setLoadingConv(true)
    try {
      const data = await api.chat.get(Number(id))
      setConversationId(data.id)
      setMessages(
        (data.messages || []).map(m => ({
          role: m.role,
          content: m.content,
          ts: m.created_at,
        }))
      )
    } catch {
      // non-fatal
    }
    setLoadingConv(false)
  }

  const send = async (textArg) => {
    const text = (textArg ?? input).trim()
    if (!text || sending) return
    setInput('')
    setSending(true)

    const now = new Date().toISOString()
    // Add user msg + empty assistant placeholder to stream into
    setMessages(prev => [
      ...prev,
      { role: 'user', content: text, ts: now },
      { role: 'assistant', content: '', ts: now },
    ])

    try {
      const res = await api.chat.stream(text, conversationId)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() // hold incomplete line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const raw = line.slice(6)
          if (raw === '[DONE]') break
          let event
          try { event = JSON.parse(raw) } catch { continue }

          if (event.type === 'token') {
            setMessages(prev => {
              const next = [...prev]
              const last = next[next.length - 1]
              next[next.length - 1] = { ...last, content: last.content + event.text }
              return next
            })
          } else if (event.type === 'done') {
            setConversationId(event.conversation_id)
            // Non-streaming intents (home control, tasks, etc.) send reply in done event
            if (event.reply) {
              setMessages(prev => {
                const next = [...prev]
                const last = next[next.length - 1]
                if (!last.content) next[next.length - 1] = { ...last, content: event.reply }
                return next
              })
            }
          }
        }
      }

      await refreshConversations()
    } catch (err) {
      setMessages(prev => {
        const next = [...prev]
        next[next.length - 1] = { ...next[next.length - 1], content: `(error: ${err.message})` }
        return next
      })
    }

    setSending(false)
    inputRef.current?.focus()
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const startRecording = async () => {
    if (recording) return
    chunksRef.current = []
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      return
    }
    streamRef.current = stream

    const mr = new MediaRecorder(stream)
    mediaRecorderRef.current = mr

    mr.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) {
        chunksRef.current.push(e.data)
      }
    }

    mr.onstop = async () => {
      // Release mic indicator immediately
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(t => t.stop())
        streamRef.current = null
      }
      const blob = new Blob(chunksRef.current, { type: 'audio/webm' })
      const file = new File([blob], 'recording.webm', { type: 'audio/webm' })
      setTranscribing(true)
      try {
        const { transcript } = await api.voice.transcribe(file)
        if (transcript && transcript.trim()) {
          await send(transcript.trim())
        }
      } catch {
        // non-fatal — user can type instead
      }
      setTranscribing(false)
    }

    mr.start()
    setRecording(true)
  }

  const stopRecording = () => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.stop()
    }
    setRecording(false)
  }

  const toggleRecording = () => {
    if (recording) {
      stopRecording()
    } else {
      startRecording()
    }
  }

  return (
    <div style={{
      width: '100%',
      maxWidth: '900px',
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
      minHeight: 'calc(100vh - 4px)',
    }}>
      {/* Page header */}
      <ScreenHeader
        section="Chat"
        title="Assistant"
        right={
          <div style={{ display: 'flex', flexDirection: 'row', gap: '10px', alignItems: 'center' }}>
            <GhostButton
              onClick={startNewChat}
              disabled={sending}
              icon={<PlusIcon />}
            >
              New chat
            </GhostButton>
            {conversations.length > 0 && (
              <select
                value={conversationId ?? ''}
                onChange={e => loadConversation(e.target.value)}
                disabled={loadingConv || sending}
                style={{
                  background: '#0c1320',
                  color: '#e9eef8',
                  border: '1px solid rgba(120,160,220,0.16)',
                  borderRadius: '9px',
                  padding: '7px 10px',
                  fontSize: '12px',
                  fontFamily: 'inherit',
                  cursor: (loadingConv || sending) ? 'not-allowed' : 'pointer',
                  opacity: (loadingConv || sending) ? 0.5 : 1,
                  maxWidth: '200px',
                  outline: 'none',
                }}
              >
                <option value="">— history —</option>
                {conversations.map(c => (
                  <option key={c.id} value={c.id}>
                    {c.title.length > 32 ? c.title.slice(0, 32) + '…' : c.title}
                  </option>
                ))}
              </select>
            )}
            {conversationId && (
              <GhostButton onClick={deleteCurrentConversation} disabled={sending}>
                Delete
              </GhostButton>
            )}
          </div>
        }
      />

      {/* Thread area */}
      <div style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
      }}>
        {/* Empty state */}
        {messages.length === 0 && !sending && (
          <div style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '16px',
            color: '#5d6982',
            textAlign: 'center',
            padding: '40px 0',
          }}>
            <div style={{
              width: '64px',
              height: '64px',
              borderRadius: '18px',
              background: 'var(--ac-dim)',
              border: '1px solid var(--ac-line)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}>
              <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.6">
                <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4L3 21l1.1-4.9A8.4 8.4 0 1 1 21 11.5z" />
              </svg>
            </div>
            <div style={{ fontSize: '15px', color: '#8a96ad', maxWidth: '380px' }}>
              Ask me anything about your homelab, or to run a task.
            </div>
          </div>
        )}

        {/* Messages */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', overflowY: 'auto', flex: 1, minHeight: 0 }}>
          {messages.map((msg, idx) => (
            <div
              key={idx}
              style={{
                display: 'flex',
                justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
              }}
            >
              <div
                style={{
                  maxWidth: '85%',
                  ...(msg.role === 'user'
                    ? {
                        background: 'var(--ac-dim)',
                        border: '1px solid var(--ac-line)',
                        borderRadius: '12px',
                        padding: '11px 14px',
                        color: '#e9eef8',
                      }
                    : {
                        background: 'rgba(255,255,255,0.022)',
                        border: '1px solid rgba(120,160,220,0.10)',
                        borderRadius: '12px',
                        padding: '14px 16px',
                        color: '#cdd6e6',
                      }),
                }}
              >
                {msg.role === 'user' ? (
                  <p style={{ margin: 0, fontSize: '14px', lineHeight: '1.5' }}>{msg.content}</p>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                    {renderMessageContent(msg.content)}
                  </div>
                )}
                {msg.ts && (
                  <p style={{ margin: '6px 0 0 0', textAlign: 'right', fontSize: '11px', color: '#5d6982' }}>
                    {formatTime(msg.ts)}
                  </p>
                )}
              </div>
            </div>
          ))}

          {sending && messages.length > 0 && messages[messages.length - 1].content === '' && (
            <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
              <div style={{
                background: 'rgba(255,255,255,0.022)',
                border: '1px solid rgba(120,160,220,0.10)',
                borderRadius: '12px',
                padding: '14px 16px',
              }}>
                <span style={{
                  fontFamily: "'JetBrains Mono', monospace",
                  color: '#5d6982',
                  fontSize: '13px',
                  fontStyle: 'italic',
                  animation: 'nx-pulse 1.4s ease-in-out infinite',
                }}>Thinking...</span>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      {/* Composer */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        padding: '8px',
        borderRadius: '14px',
        border: '1px solid rgba(120,160,220,0.14)',
        background: 'rgba(255,255,255,0.03)',
      }}>
        {hasMediaRecorder && (
          <button
            onClick={toggleRecording}
            disabled={sending || transcribing}
            title={recording ? 'Stop recording' : 'Start recording'}
            style={{
              width: '40px',
              height: '40px',
              borderRadius: '50%',
              border: recording
                ? '1px solid #fb7185'
                : '1px solid var(--ac-line)',
              background: recording
                ? 'rgba(251,113,133,0.1)'
                : 'var(--ac-dim)',
              color: recording ? '#fb7185' : 'var(--accent)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
              cursor: (sending || transcribing) ? 'not-allowed' : 'pointer',
              opacity: (sending || transcribing) ? 0.4 : 1,
              transition: 'all 0.15s ease',
            }}
          >
            {transcribing ? (
              <span style={{
                width: '8px',
                height: '8px',
                borderRadius: '50%',
                background: 'var(--accent)',
                display: 'inline-block',
                animation: 'nx-pulse 1.4s ease-in-out infinite',
              }} />
            ) : recording ? (
              <Mic size={16} style={{ animation: 'nx-pulse 1.4s ease-in-out infinite' }} />
            ) : (
              <Mic size={16} />
            )}
          </button>
        )}
        <input
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask anything..."
          disabled={sending}
          autoFocus
          style={{
            flex: 1,
            background: 'transparent',
            border: 'none',
            outline: 'none',
            color: '#e9eef8',
            fontSize: '14px',
            fontFamily: 'inherit',
          }}
        />
        <PrimaryButton
          onClick={() => send()}
          disabled={sending || !input.trim()}
          style={{ padding: '11px 20px', borderRadius: '10px' }}
        >
          {sending ? '...' : 'Send'}
        </PrimaryButton>
      </div>
    </div>
  )
}
