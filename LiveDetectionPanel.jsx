import { useEffect, useState } from 'react'
import { CheckCircle2, Power, Radio, Video, VideoOff } from 'lucide-react'
import { api, LIVE_TUNNEL_URL, resolveCameraBase } from '../api.js'
import { useAuth } from '../AuthContext.jsx'

export default function LiveDetectionPanel() {
  const { session } = useAuth()
  const [info, setInfo] = useState({ detections: [], mode: '', source: '', model: '', last_logged: null })
  const [cameraBase, setCameraBase] = useState(null)
  const [cameraOn, setCameraOn] = useState(true)
  const [broken, setBroken] = useState(false)
  const [nonce, setNonce] = useState(0)
  const [toggling, setToggling] = useState(false)

  useEffect(() => {
    let alive = true
    const syncSource = async (force = false) => {
      const target = await resolveCameraBase(force)
      if (!alive) return
      setCameraBase((prev) => {
        if (prev !== target) {
          setBroken(false)
          setNonce((n) => n + 1)
        }
        return target
      })
    }

    syncSource(true)
    const probeId = setInterval(() => syncSource(false), 8000)
    return () => {
      alive = false
      clearInterval(probeId)
    }
  }, [])

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = await api('/api/detections/current')
        if (alive) setInfo(d)
      } catch {}
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const stopCamera = async () => {
    setToggling(true)
    try {
      await api('/api/camera/stop', { method: 'POST' })
      setCameraOn(false)
    } catch {} finally {
      setToggling(false)
    }
  }

  const startCamera = async () => {
    setToggling(true)
    try {
      const target = await resolveCameraBase(true)
      setCameraBase(target)
      await api('/api/camera/start', { method: 'POST' })
      setBroken(false)
      setNonce((n) => n + 1)
      setCameraOn(true)
    } catch {} finally {
      setToggling(false)
    }
  }

  const handleStreamError = async () => {
    if (cameraBase === LIVE_TUNNEL_URL) {
      const fallback = await resolveCameraBase(true)
      if (fallback !== LIVE_TUNNEL_URL) {
        setCameraBase(fallback)
        setNonce((n) => n + 1)
        return
      }
    }
    setBroken(true)
  }

  const isLiveLaptop = cameraBase === LIVE_TUNNEL_URL
  const tokenParam = encodeURIComponent(session?.token || '')
  const src = cameraBase !== null
    ? `${cameraBase}/video_feed?token=${tokenParam}&ngrok-skip-browser-warning=true&t=${nonce}`
    : null

  const list = [...info.detections].sort((a, b) => b.confidence - a.confidence).slice(0, 7)
  const modeLabel = info.mode === 'yolov8' ? `YOLOv8 (${info.model || 'aktif'})` : 'Simulasi YOLOv8'
  const srcLabel = !cameraOn
    ? 'Kamera nonaktif'
    : info.source === 'kamera'
      ? (isLiveLaptop ? 'Webcam Laptop (Live Online)' : 'Webcam/CCTV')
      : 'Adegan simulasi'

  return (
    <div>
      <div className="relative aspect-video overflow-hidden rounded-xl bg-slate-900">
        {!cameraOn ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-4 text-center text-sm text-slate-300">
            <VideoOff size={32} className="text-slate-400" />
            <div>
              <p className="font-semibold text-white">Kamera Dihentikan</p>
              <p className="mt-0.5 text-xs text-slate-400">Webcam dalam keadaan nonaktif untuk menghemat daya.</p>
            </div>
            <button
              type="button"
              onClick={startCamera}
              disabled={toggling}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-xs font-semibold text-white transition hover:bg-primary-dark disabled:opacity-60"
            >
              <Video size={14} /> Nyalakan Kamera
            </button>
          </div>
        ) : broken ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center text-sm text-slate-300">
            <VideoOff size={28} />
            Video tidak dapat dimuat.
            <button
              type="button"
              onClick={startCamera}
              className="rounded-md bg-white/10 px-3 py-1 text-xs hover:bg-white/20"
            >
              Coba lagi
            </button>
          </div>
        ) : src ? (
          <img
            src={src}
            alt="Siaran deteksi langsung"
            className="h-full w-full object-cover"
            onError={handleStreamError}
          />
        ) : null}

        {cameraOn && !broken && src && (
          <>
            <span className="absolute left-2.5 top-9 flex items-center gap-1.5 rounded-md bg-red-600 px-2 py-0.5 text-[11px] font-bold text-white">
              <Radio size={12} className="animate-pulse" /> LIVE
            </span>

            <button
              type="button"
              onClick={stopCamera}
              disabled={toggling}
              title="Matikan kamera"
              className="absolute right-2.5 top-9 flex items-center gap-1.5 rounded-md bg-slate-950/80 px-2.5 py-1 text-[11px] font-semibold text-white backdrop-blur-sm transition hover:bg-red-600 disabled:opacity-60"
            >
              <Power size={12} /> End Camera
            </button>
          </>
        )}
      </div>
      <p className="mt-2 text-xs text-slate-500">{modeLabel} · {srcLabel}</p>

      {info.last_logged && (
        <div className="mt-3 flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          <CheckCircle2 size={15} className="shrink-0 text-emerald-600" />
          <span>
            Tercatat ke Log (Menunggu Verifikasi) & Statistik: <b>{info.last_logged.category}</b> ({info.last_logged.confidence}%) · {info.last_logged.at}
          </span>
        </div>
      )}

      <p className="mb-2 mt-4 text-[11px] font-semibold uppercase tracking-widest text-slate-500">Terdeteksi saat ini</p>
      {!cameraOn ? (
        <p className="rounded-lg bg-slate-50 px-3 py-3 text-sm text-slate-500">
          Kamera sedang mati. Klik <b>Nyalakan Kamera</b> untuk memulai deteksi objek sampah.
        </p>
      ) : list.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-3 text-sm text-slate-500">
          Kamera siaga. Arahkan objek sampah ke depan kamera untuk otomatis terdeteksi & dikirim ke Log Kejadian.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {list.map((d, i) => (
            <li key={i} className="flex items-center gap-3 rounded-lg bg-slate-50 px-3 py-2 text-sm">
              <span className="flex-1 truncate font-medium">{d.label}</span>
              <span className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
                <span className="block h-full rounded-full bg-primary" style={{ width: `${d.confidence}%` }} />
              </span>
              <span className="w-12 text-right tabular-nums text-slate-600">{d.confidence.toFixed(0)}%</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}