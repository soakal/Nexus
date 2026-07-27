import { useEffect } from 'react'

/**
 * Run `load` on mount, then on a fixed interval, and again whenever the tab
 * becomes visible or regains focus — so a backgrounded phone/laptop shows
 * fresh data the moment it comes back instead of at the next tick.
 *
 * `load` must be stable (useCallback) — it is an effect dependency, so a new
 * identity on every render would tear down and re-arm the timer each time.
 * Pass `intervalMs = 0` (or null) for a load that should only run on mount and
 * on visibility/focus, with no timer.
 *
 * @param {() => void} load
 * @param {number|null} [intervalMs]
 */
export function usePoll(load, intervalMs) {
  useEffect(() => {
    load()
    const timer = intervalMs ? setInterval(load, intervalMs) : null
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      if (timer) clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load, intervalMs])
}
