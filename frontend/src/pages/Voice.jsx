import { useState, useRef, useEffect } from 'react'
import { Mic } from 'lucide-react'
import { api } from '../lib/api'

export default function Voice() {
  const [dragging, setDragging] = useState(false)
  const [processing, setProcessing] = useState(null)
  const [result, setResult] = useState(null)
  const [recording, setRecording] = useState(false)

  const mediaRecorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)

  const hasMediaRecorder = typeof window !== 'undefined' && !!window.MediaRecorder

  const processFile = async (file) => {
    setProcessing('transcribing...')
    try {
      const r = await api.voice.upload(file)
      setResult(r)
      setProcessing(null)
    } catch (e) {
      setProcessing(null)
      setResult({ error: e.message })
    }
  }

  const startRecording = async () => {
    if (recording) return
    chunksRef.current = []
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e) {
      setResult({ error: 'mic permission denied' })
      setRecording(false)
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

    mr.onstop = () => {
      const blob = new Blob(chunksRef.current, { type: 'audio/webm' })
      const file = new File([blob], 'recording.webm', { type: 'audio/webm' })
      processFile(file)
      // Release mic indicator
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(t => t.stop())
        streamRef.current = null
      }
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

  // Cleanup on unmount
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

  return (
    <div className="p-4 md:p-6 max-w-2xl">
      <h1 className="page-header mb-6">VOICE INTERFACE</h1>

      {/* Drop zone */}
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => { e.preventDefault(); setDragging(false); const f = e.dataTransfer.files[0]; if (f) processFile(f) }}
        onClick={() => document.getElementById('voiceInput').click()}
        className="hud-panel p-8 md:p-12 text-center cursor-pointer transition-all"
        style={dragging ? { boxShadow: '0 0 30px rgba(0,212,255,0.3)' } : undefined}
      >
        <div className="flex justify-center mb-3">
          <Mic className="size-12 text-accent-cyan" style={{ filter: 'drop-shadow(0 0 8px rgba(0,212,255,0.6))' }} />
        </div>
        <p className="hud-label">DROP AUDIO FILE OR CLICK TO UPLOAD</p>
        <p className="text-text-secondary text-xs font-mono mt-2">.m4a / .wav / .mp3</p>
        <input id="voiceInput" type="file" accept=".m4a,.wav,.mp3" className="hidden"
          onChange={e => { const f = e.target.files[0]; if (f) processFile(f) }} />
      </div>

      {processing && (
        <div className="mt-4 flex items-center gap-2">
          <span className="arc-dot" />
          <span className="hud-label animate-pulse">{processing}</span>
        </div>
      )}

      {result && (
        <div className="hud-panel p-4 mt-4">
          {result.error ? (
            <div className="flex items-center gap-2">
              <span className="arc-dot-err" />
              <span className="text-accent-orange/80 text-sm">{result.error}</span>
            </div>
          ) : (
            <>
              <div className="mb-3">
                <div className="hud-label mb-1">INTENT</div>
                <div className="text-accent-cyan font-mono text-sm glow-cyan-text">{result.intent}</div>
              </div>
              <div className="mb-3">
                <div className="hud-label mb-1">TRANSCRIPT</div>
                <p className="text-text-primary text-sm">{result.transcript}</p>
              </div>
              {result.response && (
                <div>
                  <div className="hud-label mb-1">RESPONSE</div>
                  <p className="text-text-primary text-sm">{result.response}</p>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* Section divider */}
      {hasMediaRecorder && (
        <>
          <div className="section-divider my-6" style={{
            borderTop: '1px solid rgba(0,212,255,0.15)',
          }} />

          {/* Hold-to-record section */}
          <div className="flex flex-col items-center gap-4">
            <button
              onMouseDown={startRecording}
              onMouseUp={stopRecording}
              onMouseLeave={stopRecording}
              onTouchStart={e => { e.preventDefault(); startRecording() }}
              onTouchEnd={e => { e.preventDefault(); stopRecording() }}
              disabled={!!processing}
              style={{
                width: 80,
                height: 80,
                borderRadius: '50%',
                border: recording
                  ? '2px solid rgba(255,45,45,0.8)'
                  : '2px solid rgba(0,212,255,0.5)',
                background: recording
                  ? 'rgba(255,45,45,0.12)'
                  : 'rgba(0,212,255,0.08)',
                boxShadow: recording
                  ? '0 0 24px rgba(255,45,45,0.5), inset 0 0 12px rgba(255,45,45,0.15)'
                  : '0 0 16px rgba(0,212,255,0.3), inset 0 0 8px rgba(0,212,255,0.08)',
                cursor: processing ? 'not-allowed' : 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                transition: 'all 0.15s ease',
                userSelect: 'none',
                WebkitUserSelect: 'none',
              }}
            >
              {recording ? (
                <span className="arc-dot-err" />
              ) : (
                <span className="arc-dot" />
              )}
            </button>
            <p className="hud-label" style={{
              color: recording ? 'rgba(255,45,45,0.9)' : undefined,
            }}>
              {recording ? 'RECORDING — RELEASE TO SEND' : 'HOLD TO RECORD'}
            </p>
          </div>
        </>
      )}
    </div>
  )
}
