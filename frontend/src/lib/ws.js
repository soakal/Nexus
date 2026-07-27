import { API_HOST } from './api'

function deriveWsUrl() {
  if (import.meta.env.VITE_WS_BASE) {
    return import.meta.env.VITE_WS_BASE.replace(/\/$/, '') + '/ws/logs'
  }
  if (import.meta.env.VITE_API_BASE) {
    const u = new URL(import.meta.env.VITE_API_BASE)
    const proto = u.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${u.host}/ws/logs`
  }
  // HTTPS page = behind tailscale serve (same-origin /ws mount) — use wss on
  // the page host; plain HTTP (LAN) keeps the direct :8000 socket.
  if (window.location.protocol === 'https:') {
    return `wss://${window.location.host}/ws/logs`
  }
  return `ws://${API_HOST}:8000/ws/logs`
}

const WS_URL = deriveWsUrl()

let socket = null
let reconnectTimer = null
const listeners = new Set()

function reconnect() {
  if (socket) return
  if (listeners.size === 0) return

  socket = new WebSocket(WS_URL)

  socket.onmessage = (e) => {
    listeners.forEach(fn => fn(e.data))
  }

  socket.onclose = () => {
    socket = null
    if (listeners.size > 0) {
      reconnectTimer = setTimeout(reconnect, 3000)
    }
  }

  socket.onerror = () => {
    // onclose fires after onerror; let onclose handle reconnect
  }
}

function isLive() {
  return !!socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
}

function clearReconnectTimer() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

export function connectWS(onMessage) {
  listeners.add(onMessage)

  // Reuse an already open/connecting socket; otherwise clear any pending
  // reconnect timer before opening a fresh one.
  if (!isLive()) {
    clearReconnectTimer()
    reconnect()
  }

  // Unsubscribe: the last listener out closes the socket.
  return () => {
    listeners.delete(onMessage)
    if (listeners.size === 0) {
      clearReconnectTimer()
      if (isLive()) socket.close()
    }
  }
}
