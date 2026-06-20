import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import TaskCard from '../components/TaskCard'
import ScreenHeader from '../components/ScreenHeader'
import TextInput from '../components/TextInput'
import PrimaryButton from '../components/PrimaryButton'

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
    <div style={{
      width: '100%',
      maxWidth: '1100px',
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Tasks" title="Mission Control" />

      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
        <TextInput
          style={{ flex: '1 1 280px' }}
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && submit()}
          placeholder="Enter mission parameters…"
        />
        <PrimaryButton
          onClick={submit}
          disabled={loading || !prompt.trim()}
          style={{ padding: '12px 22px', borderRadius: '11px' }}
        >
          {loading ? '…' : 'Execute'}
        </PrimaryButton>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
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
