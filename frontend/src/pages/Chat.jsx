import { useState, useEffect, useRef } from 'react'
import { Mic } from 'lucide-react'
import { api } from '../lib/api'

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
        <ul key={`list-${key}`} className="list-disc list-inside text-sm leading-relaxed space-y-0.5 ml-2 my-1">
          {listItems.map((item, i) => <li key={i}>{renderInline(item)}</li>)}
        </ul>
      )
    } else if (listType === 'ol') {
      elements.push(
        <ol key={`list-${key}`} className="list-decimal list-inside text-sm leading-relaxed space-y-0.5 ml-2 my-1">
          {listItems.map((item, i) => <li key={i}>{renderInline(item)}</li>)}
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
        <p key={`h2-${idx}`} className="text-accent-cyan hud-label mt-3 mb-1">{h2Match[1]}</p>
      )
    } else if (h3Match) {
      flushList(idx)
      elements.push(
        <p key={`h3-${idx}`} className="text-text-primary text-xs font-semibold tracking-wide mt-2 mb-0.5 uppercase">{h3Match[1]}</p>
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
        <p key={`p-${idx}`} className="text-sm leading-relaxed">{renderInline(line)}</p>
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

    // Optimistic append
    setMessages(prev => [...prev, { role: 'user', content: text, ts: new Date().toISOString() }])

    try {
      const res = await api.chat.send(text, conversationId)
      setConversationId(res.conversation_id)
      setMessages(prev => [...prev, { role: 'assistant', content: res.reply, ts: new Date().toISOString() }])
      await refreshConversations()
    } catch (err) {
      setMessages(prev => [...prev, { role: 'assistant', content: `(error: ${err.message})`, ts: new Date().toISOString() }])
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
    <div
      className="p-4 md:p-6 max-w-3xl flex flex-col"
      style={{ minHeight: 'calc(100vh - 120px)' }}
    >
      {/* Page header + controls */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <h1 className="page-header mr-auto">ASSISTANT</h1>
        <button
          onClick={startNewChat}
          className="glow-btn px-3 py-1 text-xs"
          disabled={sending}
        >
          NEW CHAT
        </button>
        {conversations.length > 0 && (
          <select
            className="hud-input text-xs py-1 px-2"
            value={conversationId ?? ''}
            onChange={e => loadConversation(e.target.value)}
            disabled={loadingConv || sending}
            style={{ maxWidth: 200 }}
          >
            <option value="">— history —</option>
            {conversations.map(c => (
              <option key={c.id} value={c.id}>
                {c.title.length > 32 ? c.title.slice(0, 32) + '…' : c.title}
              </option>
            ))}
          </select>
        )}
      </div>

      {/* Thread */}
      <div className="flex-1 overflow-y-auto space-y-3 mb-4 min-h-0">
        {messages.length === 0 && !sending && (
          <div className="flex items-center justify-center h-32">
            <span className="hud-label text-text-secondary">
              Ask me anything about your homelab, or to run a task.
            </span>
          </div>
        )}

        {messages.map((msg, idx) => (
          <div
            key={idx}
            className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[85%] ${
                msg.role === 'user'
                  ? 'hud-panel-sm p-3 border border-accent-cyan/30'
                  : 'hud-panel-sm p-4'
              }`}
              style={
                msg.role === 'user'
                  ? { borderLeft: '2px solid rgba(0,212,255,0.5)', background: 'rgba(0,212,255,0.06)' }
                  : { borderLeft: '2px solid rgba(0,212,255,0.25)' }
              }
            >
              {msg.role === 'user' ? (
                <p className="text-sm text-text-primary">{msg.content}</p>
              ) : (
                <div className="space-y-1 text-text-primary">
                  {renderMessageContent(msg.content)}
                </div>
              )}
              {msg.ts && (
                <p className="hud-label mt-1.5 text-right opacity-50 text-xs">
                  {formatTime(msg.ts)}
                </p>
              )}
            </div>
          </div>
        ))}

        {sending && (
          <div className="flex justify-start">
            <div
              className="hud-panel-sm p-3"
              style={{ borderLeft: '2px solid rgba(0,212,255,0.25)' }}
            >
              <span className="hud-label animate-pulse">THINKING...</span>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input row */}
      <div className="flex gap-2 pb-4 md:pb-0">
        {hasMediaRecorder && (
          <button
            onClick={toggleRecording}
            disabled={sending || transcribing}
            title={recording ? 'Stop recording' : 'Start recording'}
            style={{
              width: 40,
              height: 40,
              borderRadius: '50%',
              border: recording
                ? '1.5px solid rgba(255,45,45,0.8)'
                : '1.5px solid rgba(0,212,255,0.5)',
              background: recording
                ? 'rgba(255,45,45,0.12)'
                : 'rgba(0,212,255,0.08)',
              boxShadow: recording
                ? '0 0 12px rgba(255,45,45,0.4)'
                : '0 0 8px rgba(0,212,255,0.2)',
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
              <span className="arc-dot animate-pulse" />
            ) : recording ? (
              <Mic size={16} className="animate-pulse" style={{ color: 'rgba(255,45,45,0.9)' }} />
            ) : (
              <Mic size={16} style={{ color: '#00d4ff' }} />
            )}
          </button>
        )}
        <input
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="ASK ANYTHING..."
          className="hud-input flex-1"
          disabled={sending}
          autoFocus
        />
        <button
          onClick={() => send()}
          disabled={sending || !input.trim()}
          className="glow-btn px-5 py-2 disabled:opacity-40"
        >
          {sending ? '...' : 'SEND'}
        </button>
      </div>
    </div>
  )
}
