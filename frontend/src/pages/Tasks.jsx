import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import TaskCard from '../components/TaskCard'

export default function Tasks() {
  const [tasks, setTasks] = useState([])
  const [prompt, setPrompt] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = () => api.tasks.list().then(setTasks).catch(() => {})

  useEffect(() => { refresh() }, [])

  // Poll while any task is running so status/results update live.
  useEffect(() => {
    const anyRunning = tasks.some(t => t.status === 'running' || t.status === 'pending')
    if (!anyRunning) return
    const id = setInterval(refresh, 3000)
    return () => clearInterval(id)
  }, [tasks])

  const submit = async () => {
    if (!prompt.trim()) return
    setLoading(true)
    try { await api.tasks.create(prompt); setPrompt(''); await refresh() } catch {}
    setLoading(false)
  }

  const handleCancel = async (id) => {
    try { await api.tasks.cancel(id); setTasks(ts => ts.filter(t => t.id !== id)) } catch {}
  }
  const handleRetry = async (id) => {
    try { await api.tasks.retry(id); await refresh() } catch {}
  }

  return (
    <div className="p-6 max-w-2xl">
      <h1 className="font-mono text-accent-cyan text-xl font-bold mb-6">TASK ORCHESTRATION</h1>
      <div className="flex gap-2 mb-6">
        <input value={prompt} onChange={e => setPrompt(e.target.value)} onKeyDown={e => e.key === 'Enter' && submit()}
          className="flex-1 bg-bg-card border border-border-dark rounded px-3 py-2 text-text-primary text-sm placeholder-text-secondary"
          placeholder="Describe your task..." />
        <button onClick={submit} disabled={loading || !prompt.trim()}
          className="bg-accent-cyan text-bg-primary font-mono text-sm px-4 py-2 rounded font-bold disabled:opacity-50">
          {loading ? '...' : 'RUN'}
        </button>
      </div>
      <div className="space-y-3">
        {tasks.map(t => <TaskCard key={t.id} task={t} onCancel={handleCancel} onRetry={handleRetry} />)}
      </div>
    </div>
  )
}
