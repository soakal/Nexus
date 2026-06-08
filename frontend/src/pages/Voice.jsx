import { useState, useEffect } from 'react'
import { api } from '../lib/api'
export default function Voice() {
  const [memos, setMemos] = useState([])
  const [dragging, setDragging] = useState(false)
  const [processing, setProcessing] = useState(null)
  const [result, setResult] = useState(null)

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

  return (
    <div className="p-6 max-w-2xl">
      <h1 className="font-mono text-accent-cyan text-xl font-bold mb-6">VOICE PIPELINE</h1>
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => { e.preventDefault(); setDragging(false); const f = e.dataTransfer.files[0]; if (f) processFile(f) }}
        onClick={() => document.getElementById('voiceInput').click()}
        className={`border-2 border-dashed rounded-lg p-12 text-center cursor-pointer transition-colors ${dragging ? 'border-accent-cyan bg-accent-cyan/5' : 'border-border-dark hover:border-accent-cyan/50'}`}
      >
        <div className="text-4xl mb-3">🎙️</div>
        <p className="text-text-secondary text-sm">Drop voice memo here or click to upload</p>
        <p className="text-text-secondary text-xs mt-1">.m4a, .wav, .mp3</p>
        <input id="voiceInput" type="file" accept=".m4a,.wav,.mp3" className="hidden"
          onChange={e => { const f = e.target.files[0]; if (f) processFile(f) }} />
      </div>
      {processing && <div className="mt-4 text-accent-cyan font-mono text-sm animate-pulse">{processing}</div>}
      {result && (
        <div className="mt-4 bg-bg-card border border-border-dark rounded-lg p-4">
          {result.error ? (
            <div className="text-accent-orange text-sm">{result.error}</div>
          ) : (
            <>
              <div className="text-accent-green text-xs font-mono mb-2">INTENT: {result.intent}</div>
              <p className="text-text-primary text-sm mb-2"><span className="text-text-secondary">Transcript: </span>{result.transcript}</p>
              {result.response && <p className="text-text-primary text-sm"><span className="text-text-secondary">Response: </span>{result.response}</p>}
            </>
          )}
        </div>
      )}
    </div>
  )
}
