"""Mesin video: webcam/CCTV + deteksi YOLOv8 otomatis, blur privasi orang, & pencatatan ke Log Kejadian."""
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import config
from categories import CATEGORIES, CATEGORY_NAMES, PLASTIC_NAMES
from database import SessionLocal
from models import Event, MonthlyStat, Point, PointCategoryStat, STATUS_FLOW

COLORS = {}
for _c in CATEGORIES:
    h = _c["color"].lstrip("#")
    COLORS[_c["name"]] = (int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16))  # BGR

_ALIAS = {n.lower().replace("/", " ").replace("_", " ").replace("-", " "): n for n in CATEGORY_NAMES}


def _find_category(*keywords: str) -> str:
    for kw in keywords:
        for name in CATEGORY_NAMES:
            if kw in name.lower():
                return name
    return CATEGORY_NAMES[0] if CATEGORY_NAMES else "Botol plastik"


# Pemetaan objek umum model standar (yolov8n.pt) ke kategori sampah ARINA
_COCO_TRASH_MAP = {
    "bottle": _find_category("botol"),
    "cup": _find_category("kemasan", "gelas", "botol"),
    "bowl": _find_category("kemasan", "styrofoam"),
    "wine glass": _find_category("kaca", "botol"),
    "vase": _find_category("kaca", "botol"),
    "banana": _find_category("organik", "sisa"),
    "apple": _find_category("organik", "sisa"),
    "orange": _find_category("organik", "sisa"),
    "sandwich": _find_category("organik", "kemasan"),
    "donut": _find_category("organik", "kemasan"),
    "cake": _find_category("organik", "kemasan"),
    "book": _find_category("kertas", "kardus", "organik"),
    "fork": _find_category("logam", "kaleng", "plastik"),
    "knife": _find_category("logam", "kaleng"),
    "spoon": _find_category("logam", "kaleng", "plastik"),
}

# Pemetaan fleksibel untuk model custom (best.pt)
_CUSTOM_KEYWORD_MAP = [
    (("botol", "bottle"), _find_category("botol")),
    (("kaleng", "can", "tin", "logam", "metal"), _find_category("kaleng", "logam")),
    (("kemasan", "sachet", "bungkus", "packaging", "cup", "bowl"), _find_category("kemasan")),
    (("kantong", "kresek", "plastic bag", "bag", "plastik"), _find_category("kantong", "plastik")),
    (("styrofoam", "gabus", "foam"), _find_category("styrofoam", "kemasan")),
    (("organik", "organic", "daun", "kayu", "wood", "ranting"), _find_category("organik", "kayu")),
]


def canonical(label: str) -> str:
    clean = label.lower().strip().replace("/", " ").replace("_", " ").replace("-", " ")
    if clean in _ALIAS:
        return _ALIAS[clean]
    for keys, target in _CUSTOM_KEYWORD_MAP:
        if any(k in clean for k in keys):
            return target
    return label


class VideoEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._stopped_by_user = False
        self._last_touch = 0.0
        self._jpeg = None
        self._detections = []
        self._updated = None
        self._cap = None
        self._model = None
        self._model_kind = "none"   # "custom" | "coco" | "none"
        self._model_name = ""
        self._tracks = []
        self._rng = random.Random()
        self._base = None
        self._streak = {}
        self._last_logged = {}
        self._last_logged_info = None
        self.mode = "simulasi"
        self.source = "sintetis"

    # ---- kontrol kamera ----
    def touch(self):
        if self._stopped_by_user:
            return
        self._last_touch = time.time()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._running = True
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()

    def stop_camera(self):
        self._stopped_by_user = True
        self._running = False
        with self._lock:
            self._detections = []
            self._jpeg = None
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
        self.source = "nonaktif"

    def start_camera(self):
        self._stopped_by_user = False
        self.touch()

    def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source,
            "model": self._model_name,
            "running": self._running and not self._stopped_by_user,
        }

    def current_detections(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "source": self.source,
                "model": self._model_name,
                "updated_at": self._updated,
                "detections": list(self._detections),
                "last_logged": self._last_logged_info,
            }

    # ---- sumber ----
    def _open(self):
        self.mode, self.source = "simulasi", "sintetis"
        self._model = None
        self._model_kind = "none"
        self._model_name = ""

        try:
            from ultralytics import YOLO

            custom_path = Path(getattr(config, "MODEL_PATH", "best.pt"))
            if custom_path.exists():
                self._model = YOLO(str(custom_path))
                self._model_kind = "custom"
                self._model_name = custom_path.name
            else:
                self._model = YOLO("yolov8n.pt")
                self._model_kind = "coco"
                self._model_name = "yolov8n.pt"
            self.mode = "yolov8"
            print(f"[ARINA] YOLOv8 aktif menggunakan model: {self._model_name}")
        except Exception as e:
            print(f"[ARINA] YOLOv8 belum tersedia ({e}); menjalankan mode simulasi.")
            self._model = None

        if self._stopped_by_user:
            return

        try:
            cap = cv2.VideoCapture(getattr(config, "CAMERA_INDEX", 0))
            if cap.isOpened():
                self._cap, self.source = cap, "kamera"
            else:
                cap.release()
        except Exception:
            self._cap = None

    def _close(self):
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
        self._running = False

    def _synthetic(self, t):
        w = getattr(config, "FRAME_W", 640)
        h = getattr(config, "FRAME_H", 480)
        if self._base is None:
            g = np.linspace(0, 1, h)[:, None, None]
            base = np.zeros((h, w, 3), np.uint8)
            top = int(h * 0.32)
            base[:top] = (96, 100, 84)
            water = np.array([150, 125, 85]) * (1 - g[top:]) + np.array([115, 95, 58]) * g[top:]
            base[top:] = np.repeat(water, w, axis=1).astype(np.uint8)
            cv2.fillPoly(base, [np.array([[0, top], [int(w * 0.2), top], [0, int(h * 0.7)]])], (58, 72, 50))
            cv2.fillPoly(base, [np.array([[w, top], [int(w * 0.78), top], [w, int(h * 0.75)]])], (58, 72, 50))
            self._base = base
        f = self._base.copy()
        for i in range(14):
            y = int(h * 0.4 + (i * 37) % (h * 0.55))
            x = int((i * 173 + t * 25 * (1 + i % 3)) % (w - 120)) + 40
            cv2.line(f, (x, y), (x + 70 + (i % 4) * 15, y), (175, 150, 110), 1, cv2.LINE_AA)
        return f

    def _read(self, t):
        w = getattr(config, "FRAME_W", 640)
        h = getattr(config, "FRAME_H", 480)
        if self._cap is not None:
            ok, frame = self._cap.read()
            if ok:
                return cv2.resize(frame, (w, h)), False
        return self._synthetic(t), True

    # ---- pemetaan & deteksi ----
    def _map_label(self, raw_name: str):
        clean = raw_name.lower().strip()
        if clean in ("person", "human", "background", "none"):
            return None
        if self._model_kind == "coco":
            return _COCO_TRASH_MAP.get(clean)
        mapped = canonical(raw_name)
        return mapped if mapped in CATEGORY_NAMES else None

    def _infer(self, frame):
        res = self._model.predict(frame, conf=0.45, verbose=False)[0]
        trash_dets = []
        person_boxes = []
        for b in res.boxes:
            raw_cls = res.names[int(b.cls[0])].lower().strip()
            conf = float(b.conf[0])
            x1, y1, x2, y2 = [int(v) for v in b.xyxy[0].tolist()]

            if raw_cls in ("person", "human") and conf >= 0.45:
                person_boxes.append([x1, y1, x2, y2])
                continue

            if conf < 0.50:
                continue

            label = self._map_label(raw_cls)
            if not label:
                continue

            trash_dets.append({
                "label": label,
                "confidence": round(conf * 100, 1),
                "box": [x1, y1, x2, y2],
            })
        return trash_dets, person_boxes

    def _simulate(self, shape):
        h, w = shape[:2]
        r = self._rng
        while len(self._tracks) < 5 and r.random() < 0.10:
            self._tracks.append({
                "label": r.choice(CATEGORY_NAMES),
                "conf": r.uniform(62, 95),
                "cx": r.uniform(0.2, 0.8) * w,
                "cy": r.uniform(0.45, 0.85) * h,
                "w": r.uniform(40, 110),
                "h": r.uniform(30, 80),
                "vx": r.uniform(-1.4, 1.4),
                "vy": r.uniform(0.1, 0.9),
                "ttl": r.randint(80, 240),
            })
        dets = []
        for tr in self._tracks:
            tr["cx"] += tr["vx"]
            tr["cy"] += tr["vy"]
            tr["ttl"] -= 1
            tr["conf"] = min(98, max(55, tr["conf"] + r.uniform(-0.6, 0.6)))
            x1, y1 = int(tr["cx"] - tr["w"] / 2), int(tr["cy"] - tr["h"] / 2)
            dets.append({
                "label": tr["label"],
                "confidence": round(tr["conf"], 1),
                "box": [max(0, x1), max(0, y1), min(w - 1, int(x1 + tr["w"])), min(h - 1, int(y1 + tr["h"]))],
            })
        self._tracks = [t for t in self._tracks if t["ttl"] > 0 and 0 < t["cx"] < w and t["cy"] < h * 0.96]
        return dets, []

    def _draw(self, frame, dets, synthetic, privacy_badge: bool = False):
        out = frame.copy()
        for d in dets:
            x1, y1, x2, y2 = d["box"]
            col = COLORS.get(d["label"], (42, 160, 255))
            if synthetic:
                cv2.ellipse(
                    out,
                    ((x1 + x2) // 2, (y1 + y2) // 2),
                    (max(4, (x2 - x1) // 2 - 6), max(4, (y2 - y1) // 2 - 6)),
                    0, 0, 360, col, -1,
                )
            cv2.rectangle(out, (x1, y1), (x2, y2), col, 2)
            txt = f"{d['label']} {d['confidence']:.0f}%"
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            top_y = max(th + 10, y1)
            cv2.rectangle(out, (x1, top_y - th - 8), (x1 + tw + 10, top_y), col, -1)
            cv2.putText(out, txt, (x1 + 5, top_y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)

        header = f"YOLOv8 ({self._model_name})" if (self.mode == "yolov8" and not synthetic) else "SIMULASI YOLOv8"
        suffix = " | ORANG DI-BLUR" if privacy_badge else ""
        tag = f"{header} | {datetime.now():%d/%m/%Y %H:%M:%S}{suffix}"
        cv2.rectangle(out, (0, 0), (out.shape[1], 28), (22, 32, 38), -1)
        cv2.putText(out, tag, (10, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (235, 245, 255), 1, cv2.LINE_AA)
        return out

    def _build_blurred_snapshot(self, raw_frame, dets, person_boxes):
        """Memburamkan orang/pemegang sampah di foto bukti, tetapi menjaga objek sampah tetap jelas."""
        h, w = raw_frame.shape[:2]
        snap = raw_frame.copy()
        has_person = len(person_boxes) > 0

        if has_person:
            for px1, py1, px2, py2 in person_boxes:
                x1, y1 = max(0, px1), max(0, py1)
                x2, y2 = min(w, px2), min(h, py2)
                if x2 > x1 and y2 > y1:
                    roi = snap[y1:y2, x1:x2]
                    snap[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (99, 99), 30)

            # Kembalikan area objek sampah agar tidak ikut kabur saat dipegang
            for d in dets:
                bx1, by1, bx2, by2 = d["box"]
                pad = 8
                x1, y1 = max(0, bx1 - pad), max(0, by1 - pad)
                x2, y2 = min(w, bx2 + pad), min(h, by2 + pad)
                if x2 > x1 and y2 > y1:
                    snap[y1:y2, x1:x2] = raw_frame[y1:y2, x1:x2]

        return self._draw(snap, dets, synthetic=False, privacy_badge=has_person), has_person

    # ---- simpan otomatis ke Log Kejadian (Menunggu Verifikasi) & Statistik ----
    def _record_if_stable(self, raw_frame, dets, person_boxes):
        if self.mode != "yolov8":
            return

        seen_now = {}
        for d in dets:
            lbl = d["label"]
            if lbl not in seen_now or d["confidence"] > seen_now[lbl]:
                seen_now[lbl] = d["confidence"]

        for lbl in list(self._streak.keys()):
            if lbl not in seen_now:
                self._streak[lbl] = 0

        now = time.time()
        cooldown_sec = 10.0

        for lbl, conf in seen_now.items():
            self._streak[lbl] = self._streak.get(lbl, 0) + 1
            if self._streak[lbl] < 2:
                continue
            if now - self._last_logged.get(lbl, 0.0) < cooldown_sec:
                continue

            self._last_logged[lbl] = now
            snapshot_img, has_person = self._build_blurred_snapshot(raw_frame, dets, person_boxes)
            self._save_to_db(snapshot_img, lbl, conf, has_person)

    def _save_to_db(self, snapshot_frame, category: str, confidence: float, has_person: bool):
        try:
            with SessionLocal() as db:
                pt = db.query(Point).filter(Point.code == "T1").first() or db.query(Point).order_by(Point.code).first()
                if not pt:
                    return

                snap_name = None
                try:
                    snap_dir = Path(config.STATIC_DIR) / "snapshots"
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    snap_name = f"yolo_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
                    cv2.imwrite(str(snap_dir / snap_name), snapshot_frame)
                except Exception:
                    snap_name = None

                note_text = (
                    f"Deteksi otomatis YOLOv8 ({self._model_name}) · Sosok pemegang sampah disamarkan (blur)"
                    if has_person
                    else f"Deteksi otomatis YOLOv8 ({self._model_name}) via webcam"
                )

                ev = Event(
                    point_code=pt.code,
                    occurred_at=datetime.now(),
                    category=category,
                    count=1,
                    density=pt.density,
                    confidence=round(confidence, 1),
                    status=STATUS_FLOW[0],  # Masuk sebagai data Menunggu Verifikasi
                    snapshot=snap_name,
                    note=note_text,
                )
                db.add(ev)

                stat = (
                    db.query(PointCategoryStat)
                    .filter(PointCategoryStat.point_code == pt.code, PointCategoryStat.category == category)
                    .first()
                )
                if stat:
                    stat.count += 1
                else:
                    db.add(PointCategoryStat(point_code=pt.code, category=category, count=1))

                m_stat = db.query(MonthlyStat).order_by(MonthlyStat.order.desc()).first()
                if m_stat:
                    if category in PLASTIC_NAMES:
                        m_stat.plastic += 1
                    else:
                        m_stat.non_plastic += 1

                db.commit()
                with self._lock:
                    self._last_logged_info = {
                        "category": category,
                        "confidence": round(confidence, 1),
                        "point": pt.code,
                        "at": datetime.now().strftime("%H:%M:%S"),
                    }
                print(f"[ARINA] Tercatat ke Log (Menunggu Verifikasi): {category} ({confidence:.1f}%) | blur_orang={has_person}")
        except Exception as e:
            print(f"[ARINA] Gagal menyimpan deteksi ke database: {e}")

    # ---- loop ----
    def _loop(self):
        self._open()
        interval = 1.0 / getattr(config, "TARGET_FPS", 15)
        idle_timeout = getattr(config, "IDLE_TIMEOUT", 60)
        jpeg_quality = getattr(config, "JPEG_QUALITY", 80)
        try:
            while self._running and not self._stopped_by_user and time.time() - self._last_touch < idle_timeout:
                t0 = time.time()
                raw_frame, synthetic = self._read(t0)
                if self._stopped_by_user:
                    break

                if self.mode == "yolov8" and self._model and not synthetic:
                    dets, person_boxes = self._infer(raw_frame)
                else:
                    dets, person_boxes = self._simulate(raw_frame.shape)

                live_annotated = self._draw(raw_frame, dets, synthetic)
                self._record_if_stable(raw_frame, dets, person_boxes)

                ok, buf = cv2.imencode(".jpg", live_annotated, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                if ok:
                    with self._lock:
                        self._jpeg = buf.tobytes()
                        self._detections = [{"label": d["label"], "confidence": d["confidence"]} for d in dets]
                        self._updated = datetime.now().isoformat(timespec="seconds")
                time.sleep(max(0, interval - (time.time() - t0)))
        finally:
            self._close()

    def mjpeg(self):
        last = None
        while not self._stopped_by_user:
            self.touch()
            with self._lock:
                jpeg = self._jpeg
            if jpeg is not None and jpeg is not last:
                last = jpeg
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            time.sleep(0.03)


engine = VideoEngine()