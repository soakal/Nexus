import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// One-tap device onboarding: a link like http://host:3000/?key=XXXX saves the
// API key into THIS browser's localStorage, then strips it from the URL so the
// secret doesn't linger in the address bar / history.
const _params = new URLSearchParams(window.location.search)
const _k = _params.get('key')
if (_k && _k.trim()) {
  localStorage.setItem('nexus_api_key', _k.trim())
  _params.delete('key')
  const _qs = _params.toString()
  window.history.replaceState(
    {},
    '',
    window.location.pathname + (_qs ? `?${_qs}` : '') + window.location.hash,
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
