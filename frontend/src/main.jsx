import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// Device onboarding: open Settings and paste the API key (over the tailnet's
// HTTPS). The old ?key= link scheme is retired — keys don't belong in URLs.

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
