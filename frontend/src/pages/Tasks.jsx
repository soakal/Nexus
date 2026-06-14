import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import TaskCard from '../components/TaskCard'

export default function Tasks() {
  const [tasks, setTasks] = useState([])
  const [prompt, setPrompt] = useState('')
  const [loading, setLoading] = useState(false)
  // confirmDelete: object keyed by task id, value true when awaiting confirmation
  const [confirmDelete, setConfirmDelete] = useState({})

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
    const task = tasks.find(t => t.id === id)
    const isRunning = task && (task.status === 'running' || task.status === 'pending')

    if (isRunning) {
      // Running/pending: cancel immediately, no confirmation needed
      try { await api.tasks.cancel(id); setTasks(ts => ts.filter(t => t.id !== id)) } catch {}
    } else {
      // Completed/failed/other: require two-click confirmation
      if (!confirmDelete[id]) {
        // First click: enter confirm-pending state
        setConfirmDelete(prev => ({ ...prev, [id]: true }))
      } else {
        // Second click: proceed with actual delete
        try {
          await api.tasks.cancel(id)
          setTasks(ts => ts.filter(t => t.id !== id))
        } catch {}
        setConfirmDelete(prev => {
          const next = { ...prev }
          delete next[id]
          return next
        })
      }
    }
  }

  const handleAbortDelete = (id) => {
    setConfirmDelete(prev => {
      const next = { ...prev }
      delete next[id]
      return next
    })
  }

  const handleRetry = async (id) => {
    try { await api.tasks.retry(id); await refresh() } catch {}
  }

  return (
    <div className="p-4 md:p-6 max-w-2xl space-y-6">
      <h1 className="page-header">MISSION CONTROL</h1>
      <div className="flex flex-wrap gap-2">
        <input value={prompt} onChange={e => setPrompt(e.target.value)} onKeyDown={e => e.key === 'Enter' && submit()}
          className="hud-input flex-1"
          placeholder="ENTER MISSION PARAMETERS..." />
        <button onClick={submit} disabled={loading || !prompt.trim()}
          className="glow-btn px-5 py-2 disabled:opacity-40">
          {loading ? '...' : 'EXECUTE'}
        </button>
      </div>
      <div className="space-y-3">
        {tasks.map(t => (
          <TaskCard
            key={t.id}
            task={t}
            onCancel={handleCancel}
            onRetry={handleRetry}
            confirmPending={!!confirmDelete[t.id]}
            onAbortDelete={handleAbortDelete}
          />
        ))}
      </div>
    </div>
  )
}
