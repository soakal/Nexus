const BASE = `http://127.0.0.1:8000/api`

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
    }
  },
}
