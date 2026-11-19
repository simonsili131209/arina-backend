// Base URL kosong = pakai proxy Vite. Saat deploy, set VITE_API_URL (mis. https://api.arina.id)
export const BASE = import.meta.env.VITE_API_URL || ''
export const LIVE_TUNNEL_URL = 'https://barrel-smirk-payroll.ngrok-free.dev'
const KEY = 'arina_session'

let _cachedCameraBase = null
let _lastProbeAt = 0

export const assetUrl = (p) => {
  if (!p) return null
  if (p.startsWith('data:') || p.startsWith('http://') || p.startsWith('https://')) return p
  return `${BASE}${p}`
}

export async function resolveCameraBase(force = false) {
  const now = Date.now()
  if (!force && _cachedCameraBase !== null && now - _lastProbeAt < 8000) {
    return _cachedCameraBase
  }
  _lastProbeAt = now

  // Jika dibuka langsung di localhost laptop, pakai BASE lokal
  if (typeof window !== 'undefined' && ['localhost', '127.0.0.1'].includes(window.location.hostname)) {
    _cachedCameraBase = BASE
    return _cachedCameraBase
  }

  try {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 2000)
    const res = await fetch(`${LIVE_TUNNEL_URL}/api/health`, {
      signal: ctrl.signal,
      headers: { 'ngrok-skip-browser-warning': 'true' },
    })
    clearTimeout(timer)
    if (res.ok) {
      const info = await res.json()
      if (info?.status === 'ok') {
        _cachedCameraBase = LIVE_TUNNEL_URL
        return _cachedCameraBase
      }
    }
  } catch {
    // Tunnel laptop sedang mati -> otomatis jatuh ke server simulasi 24 jam
  }

  _cachedCameraBase = BASE
  return _cachedCameraBase
}

export function loadSession() {
  try { return JSON.parse(localStorage.getItem(KEY)) } catch { return null }
}
export function saveSession(s) { try { localStorage.setItem(KEY, JSON.stringify(s)) } catch {} }
export function clearSession() { try { localStorage.removeItem(KEY) } catch {} }

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status }
}

const LIVE_PATHS = ['/api/detections/', '/api/camera/', '/api/events']

export async function api(path, { method = 'GET', body } = {}) {
  const s = loadSession()
  const isLiveRoute = LIVE_PATHS.some((prefix) => path.startsWith(prefix))
  const targetBase = isLiveRoute ? await resolveCameraBase() : BASE

  let res
  try {
    res = await fetch(targetBase + path, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'ngrok-skip-browser-warning': 'true',
        ...(s ? { Authorization: `Bearer ${s.token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError('Tidak dapat terhubung ke server ARINA. Pastikan backend berjalan di port 8000.', 0)
  }
  let data = null
  try { data = await res.json() } catch {}
  if (!res.ok) {
    if (res.status === 401 && s) window.dispatchEvent(new Event('arina:unauthorized'))
    throw new ApiError(data?.detail || `Terjadi kesalahan (${res.status})`, res.status)
  }
  return data
}