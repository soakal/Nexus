const _host = import.meta.env.VITE_API_BASE
  ? new URL(import.meta.env.VITE_API_BASE).hostname
  : (window.location.hostname || '127.0.0.1')

export const API_HOST = _host

const _base = import.meta.env.VITE_API_BASE
  ? import.meta.env.VITE_API_BASE.replace(/\/$/, '')
  : `${window.location.protocol}//${API_HOST}:8000`

export const API_BASE = _base
export const WS_BASE = _base.replace(/^http/, 'ws')

const BASE = `${_base}/api`

function getKey() {
  return localStorage.getItem('nexus_api_key') || ''
}

async function req(method, path, body) {
  const headers = { 'Content-Type': 'application/json', 'Authorization': `Bearer ${getKey()}` }
  const opts = { method, headers }
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(`${BASE}${path}`, opts)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  const text = await res.text()
  return text ? JSON.parse(text) : null
}

export const api = {
  get: (path) => req('GET', path),
  post: (path, body) => req('POST', path, body),
  health: () => req('GET', '/health'),
  briefing: {
    trigger: () => req('POST', '/briefing/trigger'),
    latest: () => req('GET', '/briefing/latest'),
    list: () => req('GET', '/briefing/'),
  },
  tasks: {
    create: (prompt) => req('POST', '/tasks/', { prompt }),
    list: () => req('GET', '/tasks/'),
    get: (id) => req('GET', `/tasks/${id}`),
    cancel: (id) => req('DELETE', `/tasks/${id}`),
    retry: (id) => req('POST', `/tasks/${id}/retry`),
  },
  sources: { status: () => req('GET', '/sources/status') },
  agents: { runs: (q) => req('GET', `/agents/runs${q ? `?q=${encodeURIComponent(q)}` : ''}`) },
  channels: { get: () => req('GET', '/channels/'), record: (id) => req('POST', '/channels/record', { program_id: id }) },
  adguard: {
    get: () => req('GET', '/adguard/'),
    toggle: (enabled) => req('POST', '/adguard/filter', { enabled }),
    timedDisable: (minutes) => req('POST', '/adguard/disable-timed', { minutes }),
  },
  trends: { get: (source, metric, days) => req('GET', `/trends/${source}/${metric}?days=${days || 7}`) },
  uptime: {
    summary: (days) => req('GET', `/uptime/summary?days=${days || 7}`),
    history: (source, days) => req('GET', `/uptime/history/${source}?days=${days || 7}`),
    speedtest: (days) => req('GET', `/uptime/speedtest?days=${days || 7}`),
  },
  secrets: {
    list: () => req('GET', '/secrets/list'),
    set: (key, value) => req('POST', '/secrets/set', { key, value }),
    test: (key) => req('POST', `/secrets/test/${key}`),
  },
  unraid: { get: () => req('GET', '/unraid/'), restartDocker: (id) => req('POST', `/unraid/docker/${id}/restart`) },
  ha: {
    entities: () => req('GET', '/ha/entities'),
    service: (domain, service, entity_id) => req('POST', '/ha/service', { domain, service, entity_id }),
  },
  voice: {
    upload: async (file) => {
      const fd = new FormData()
      fd.append('file', file)
      const res = await fetch(`${BASE}/voice/upload`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${getKey()}` },
        body: fd,
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    transcribe: async (file) => {
      const fd = new FormData()
      fd.append('file', file)
      const res = await fetch(`${BASE}/voice/transcribe`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${getKey()}` },
        body: fd,
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
  },
  chat: {
    send: (message, conversationId) => req('POST', '/chat/', { message, conversation_id: conversationId ?? null }),
    conversations: () => req('GET', '/chat/conversations'),
    get: (id) => req('GET', `/chat/${id}`),
    remove: (id) => req('DELETE', `/chat/${id}`),
  },
  today: { get: () => req('GET', '/today/') },
  safety: {
    status: () => req('GET', '/safety/status'),
    pause: () => req('POST', '/safety/pause'),
    resume: () => req('POST', '/safety/resume'),
    setBudget: (daily_usd, per_task_usd) => req('POST', '/safety/budget', { daily_usd, per_task_usd }),
    actions: (limit) => req('GET', `/safety/actions?limit=${limit || 20}`),
    outcomes: (limit) => req('GET', `/safety/outcomes?limit=${limit || 20}`),
    metering: () => req('GET', '/safety/metering'),
    confirmAction: (id) => req('POST', `/safety/actions/${id}/confirm`),
    pendingActions: (limit) => req('GET', `/safety/actions?decision=needs_confirm&limit=${limit || 20}`),
  },
  goals: {
    list: () => req('GET', '/goals/'),
    propose: (title, description, risk) => req('POST', '/goals/propose', { title, description, risk: risk || 'medium' }),
    approve: (id) => req('POST', `/goals/${id}/approve`),
    reject: (id) => req('POST', `/goals/${id}/reject`),
  },
}
