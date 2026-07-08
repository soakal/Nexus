const _host = import.meta.env.VITE_API_BASE
  ? new URL(import.meta.env.VITE_API_BASE).hostname
  : (window.location.hostname || '127.0.0.1')

export const API_HOST = _host

// Runtime branch (C6): over HTTPS we're behind `tailscale serve`, which mounts
// /api and /ws on the SAME origin — use same-origin so the browser never makes
// a mixed-content http://:8000 call. Over plain HTTP (LAN) keep hitting :8000
// directly. VITE_API_BASE stays the highest-priority override.
const _base = import.meta.env.VITE_API_BASE
  ? import.meta.env.VITE_API_BASE.replace(/\/$/, '')
  : window.location.protocol === 'https:'
    ? ''
    : `${window.location.protocol}//${API_HOST}:8000`

export const API_BASE = _base
export const WS_BASE = _base
  ? _base.replace(/^http/, 'ws')
  : `wss://${window.location.host}`

// The live-feed WS URL (no key in the URL — see wsLogsProtocols).
export function wsLogsUrl() {
  return `${WS_BASE}/ws/logs`
}

// Pass the API key as a WebSocket subprotocol instead of a query param so it
// never appears in the URL (and therefore never in server access logs). The
// server validates subprotocols[1] after the "nexus-api-key" sentinel and echoes
// only the sentinel back. Returns [] when no key is set (handshake will be rejected).
export function wsLogsProtocols() {
  const k = localStorage.getItem('nexus_api_key') || ''
  return k ? ['nexus-api-key', k] : []
}

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
  briefing: {
    trigger: () => req('POST', '/briefing/trigger'),
    latest: () => req('GET', '/briefing/latest'),
  },
  tasks: {
    create: (prompt) => req('POST', '/tasks/', { prompt }),
    list: () => req('GET', '/tasks/'),
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
  uptime: {
    summary: (days) => req('GET', `/uptime/summary?days=${days || 7}`),
    speedtest: (days) => req('GET', `/uptime/speedtest?days=${days || 7}`),
  },
  secrets: {
    list: () => req('GET', '/secrets/list'),
    set: (key, value) => req('POST', '/secrets/set', { key, value }),
    test: (key) => req('POST', `/secrets/test/${key}`),
    delete: (key) => req('DELETE', `/secrets/${key}`),
    backup: () => req('POST', '/secrets/backup'),
    credentials: {
      list: () => req('GET', '/secrets/credentials'),
      set: (body) => req('POST', '/secrets/credentials', body),
      delete: (service) => req('DELETE', `/secrets/credentials/${service}`),
    },
  },
  unraid: { get: () => req('GET', '/unraid/'), restartDocker: (id) => req('POST', `/unraid/docker/${id}/restart`) },
  ha: {
    entities: () => req('GET', '/ha/entities'),
    service: (domain, service, entity_id, service_data) =>
      req('POST', '/ha/service', { domain, service, entity_id, ...(service_data ? { service_data } : {}) }),
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
    stream: (message, conversationId) => fetch(`${BASE}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${getKey()}` },
      body: JSON.stringify({ message, conversation_id: conversationId ?? null }),
    }).then(res => { if (!res.ok) throw new Error(res.status); return res }),
    conversations: () => req('GET', '/chat/conversations'),
    get: (id) => req('GET', `/chat/${id}`),
  },
  today: {
    get: () => req('GET', '/today/'),
    homeState: () => req('GET', '/today/home-state'),
  },
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
    clearDeadLetters: () => req('DELETE', '/safety/deliveries/dead'),
  },
  goals: {
    list: (category) => req('GET', `/goals/${category ? `?category=${encodeURIComponent(category)}` : ''}`),
    categories: () => req('GET', '/goals/categories'),
    propose: (title, description, risk, category, cadence, successCriteria) => req('POST', '/goals/propose', { title, description, risk: risk || 'medium', category: category || 'other', cadence: cadence || null, success_criteria: successCriteria || null }),
    approve: (id) => req('POST', `/goals/${id}/approve`),
    reject: (id) => req('POST', `/goals/${id}/reject`),
    edit: (id, fields) => req('PATCH', `/goals/${id}`, fields),
    remove: (id) => req('DELETE', `/goals/${id}`),
    disable: (id) => req('POST', `/goals/${id}/disable`),
    enable: (id) => req('POST', `/goals/${id}/enable`),
  },
  brain: {
    status: () => req('GET', '/brain-organizer/status'),
    run: () => req('POST', '/brain-organizer/run'),
    resetFailed: () => req('POST', '/brain-organizer/reset-failed'),
  },
  facts: {
    list: () => req('GET', '/facts/'),
    recall: (q) => req('GET', `/facts/recall?query=${encodeURIComponent(q)}`),
    dismiss: (id) => req('POST', `/facts/${id}/dismiss`),
  },
}
