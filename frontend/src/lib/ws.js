let socket = null
const listeners = new Set()

export function connectWS(onMessage) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    listeners.add(onMessage)
    return () => listeners.delete(onMessage)
  }
  socket = new WebSocket(`ws://127.0.0.1:8000/ws/logs`)
  socket.onmessage = (e) => listeners.forEach(fn => fn(e.data))
  socket.onclose = () => { socket = null; setTimeout(() => connectWS(onMessage), 3000) }
  listeners.add(onMessage)
  return () => listeners.delete(onMessage)
}
