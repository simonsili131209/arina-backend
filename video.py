"""Mesin video Hybrid Fast-Response (YOLOv8n + best.pt v2): deteksi multi-sampah, anti-wajah, & simpan bukti langsung ke DB."""
import base64
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


def _find_category(*keywords: str) -> str:
    for kw in keywords:
        for name in CATEGORY_NAMES:
            if kw in name.lower():
                return name
    return CATEGORY_NAMES[0] if CATEGORY_NAMES else "Botol plastik"


# 1. Pemetaan kelas umum (yolov8n.pt) -> kategori ARINA
_COCO_TRASH_MAP = {
    "bottle": _find_category("botol"),
    "wine glass": _find_category("botol", "gelas"),
    "cup": _find_category("gelas", "kemasan"),
    "bowl": _find_category("kemasan", "styrofoam"),
    "fork": _find_category("kaleng"),
    "knife": _find_category("kaleng"),
    "spoon": _find_category("kaleng"),
    "banana": _find_category("organik"),
    "apple": _find_category("organik"),
    "orange": _find_category("organik"),
    "broccoli": _find_category("organik"),
    "carrot": _find_category("organik"),
    "sandwich": _find_category("organik"),
    "donut": _find_category("organik"),
    "cake": _find_category("organik"),
    "book": _find_category("kertas", "kardus"),
}

# 2. Pemetaan 6 kelas model custom (best.pt) -> kategori ARINA
_CUSTOM_TRASH_MAP = {
    "biodegradable": _find_category("organik"),
    "cardboard": _find_category("kertas", "kardus"),
    "paper": _find_category("kertas", "kardus"),
    "glass": _find_category("botol"),
    "metal": _find_category("kaleng"),
    "plastic": _find_category("kantong"),
}


def _box_iou(a, b) -> float:
    xA = max(a[0], b[0])
    yA = max(a[1], b[1])
    xB = min(a[2], b[2])
    yB = min(a[3], b[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter <= 0:
        return 0.0
    areaA = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    areaB = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / float(areaA + areaB - inter)


def _overlap_ratio(inner, outer) -> float:
    xA = max(inner[0], outer[0])
    yA = max(inner[1], outer[1])
    xB = min(inner[2], outer[2])
    yB = min(inner[3], outer[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    area_inner = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return inter / float(area_inner)


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
        self._coco_model = None
        self._custom_model = None
        self._face_frontal = None
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

    # ---- sumber & pemuatan model ----
    def _find_file(self, filename: str):
        backend_dir = Path(__file__).resolve().parent
        for p in [backend_dir / filename, Path(filename)]:
            if p.exists() and p.is_file():
                return p
        return None

    def _open(self):
        self.mode, self.source = "simulasi", "sintetis"
        self._coco_model = None
        self._custom_model = None
        self._model_name = ""

        try:
            f1 = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if not f1.empty():
                self._face_frontal = f1
        except Exception:
            self._face_frontal = None

        try:
            from ultralytics import YOLO

            coco_path = self._find_file("yolov8n.pt")
            custom_path = self._find_file("best.pt")

            self._coco_model = YOLO(str(coco_path)) if coco_path else YOLO("yolov8n.pt")
            if custom_path is not None:
                self._custom_model = YOLO(str(custom_path))
                self._model_name = "Hybrid (best.pt + yolov8n)"
            else:
                self._model_name = "yolov8n.pt"

            self.mode = "yolov8"
            print(f"[ARINA] Deteksi AI Fast-Mode aktif: {self._model_name}")
        except Exception as e:
            print(f"[ARINA] YOLOv8 gagal dimuat ({e}); menjalankan mode simulasi.")
            self._coco_model = None
            self._custom_model = None

        if self._stopped_by_user:
            return

        try:
            cap = cv2.VideoCapture(getattr(config, "CAMERA_INDEX", 0))
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
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
            self._cap.grab()
            ok, frame = self._cap.retrieve()
            if not ok:
                ok, frame = self._cap.read()
            if ok:
                return cv2.resize(frame, (w, h)), False
        return self._synthetic(t), True

    # ---- pelindung wajah, kepala, & warna kulit ----
    def _detect_faces(self, frame):
        boxes = []
        if self._face_frontal is None:
            return boxes
        small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        detected = self._face_frontal.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=3, minSize=(22, 22))
        for (fx, fy, fw, fh) in detected:
            boxes.append([fx * 2, fy * 2, (fx + fw) * 2, (fy + fh) * 2])
        return boxes

    def _skin_ratio(self, frame, box) -> float:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
        bw, bh = x2 - x1, y2 - y1
        if bw < 12 or bh < 12:
            return 0.0
        ix1, iy1 = x1 + int(bw * 0.22), y1 + int(bh * 0.15)
        ix2, iy2 = x2 - int(bw * 0.22), y2 - int(bh * 0.25)
        crop = frame[iy1:iy2, ix1:ix2]
        if crop.size == 0:
            return 0.0
        ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycrcb, np.array([0, 133, 77], dtype=np.uint8), np.array([255, 173, 127], dtype=np.uint8))
        return float(np.count_nonzero(mask)) / float(mask.size)

    def _is_human_false_positive(self, frame, box, face_boxes, person_boxes) -> bool:
        bx1, by1, bx2, by2 = box
        cx, cy = (bx1 + bx2) // 2, (by1 + by2) // 2

        for fbox in face_boxes:
            if fbox[0] <= cx <= fbox[2] and fbox[1] <= cy <= fbox[3]:
                return True
            if _overlap_ratio(fbox, box) > 0.45:
                return True

        for pbox in person_boxes:
            pw = max(1, pbox[2] - pbox[0])
            ph = max(1, pbox[3] - pbox[1])
            in_head_x = (pbox[0] + pw * 0.15) <= cx <= (pbox[2] - pw * 0.15)
            in_head_y = pbox[1] <= cy <= (pbox[1] + ph * 0.55)
            starts_at_head = by1 <= (pbox[1] + ph * 0.35)
            if in_head_x and in_head_y and starts_at_head:
                return True

        if self._skin_ratio(frame, box) > 0.32:
            return True

        return False

    def _infer(self, frame):
        person_boxes = []
        face_boxes = self._detect_faces(frame)
        candidates = []

        # Tahap A: Model Umum (yolov8n) -> manusia, botol kaku, gelas, alat makan/logam, buah/organik
        if self._coco_model is not None:
            res_coco = self._coco_model.predict(frame, conf=0.22, imgsz=416, verbose=False)[0]
            for b in res_coco.boxes:
                raw_cls = res_coco.names[int(b.cls[0])].lower().strip()
                conf = float(b.conf[0])
                box = [int(v) for v in b.xyxy[0].tolist()]

                if raw_cls == "person" and conf >= 0.25:
                    person_boxes.append(box)
                    continue

                if raw_cls in _COCO_TRASH_MAP:
                    # Naikkan ambang 'bottle' ke 0.52 agar kresek lemas tidak keliru dianggap botol
                    if raw_cls == "bottle":
                        min_req = 0.52
                        prio = 2 if conf >= 0.62 else 1
                    elif raw_cls in ("spoon", "fork", "knife"):
                        min_req = 0.24
                        prio = 2
                    else:
                        min_req = 0.36
                        prio = 2

                    if conf >= min_req:
                        candidates.append({
                            "label": _COCO_TRASH_MAP[raw_cls],
                            "confidence": round(conf * 100, 1),
                            "box": box,
                            "priority": prio,
                        })

        # Tahap B: Model Custom (best.pt) -> kantong plastik (kresek), kardus/kertas, organik, kaleng
        if self._custom_model is not None:
            res_cust = self._custom_model.predict(frame, conf=0.38, imgsz=416, verbose=False)[0]
            for b in res_cust.boxes:
                raw_cls = res_cust.names[int(b.cls[0])].lower().strip()
                conf = float(b.conf[0])
                box = [int(v) for v in b.xyxy[0].tolist()]

                mapped = _CUSTOM_TRASH_MAP.get(raw_cls)
                if not mapped:
                    continue

                min_cust = 0.58 if raw_cls == "biodegradable" else 0.38
                prio = 2 if (raw_cls == "plastic" and conf >= 0.44) else 1
                if conf >= min_cust:
                    candidates.append({
                        "label": mapped,
                        "confidence": round(conf * 100, 1),
                        "box": box,
                        "priority": prio,
                    })

        # Tahap C: Saring wajah/kepala/kulit & gabungkan kotak bertumpuk
        filtered = [
            c for c in candidates
            if not self._is_human_false_positive(frame, c["box"], face_boxes, person_boxes)
        ]
        filtered.sort(key=lambda x: (x["priority"], x["confidence"]), reverse=True)

        final_dets = []
        for item in filtered:
            if any(_box_iou(item["box"], kept["box"]) > 0.45 for kept in final_dets):
                continue
            final_dets.append({
                "label": item["label"],
                "confidence": item["confidence"],
                "box": item["box"],
            })

        return final_dets, person_boxes

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
        cv2.putText(out, tag, (10, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (235, 245, 255), 1, cv2.LINE_AA)
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

            for d in dets:
                bx1, by1, bx2, by2 = d["box"]
                pad = 8
                x1, y1 = max(0, bx1 - pad), max(0, by1 - pad)
                x2, y2 = min(w, bx2 + pad), min(h, by2 + pad)
                if x2 > x1 and y2 > y1:
                    snap[y1:y2, x1:x2] = raw_frame[y1:y2, x1:x2]

        return self._draw(snap, dets, synthetic=False, privacy_badge=has_person), has_person

    def _encode_snapshot_for_db(self, snapshot_frame) -> str | None:
        """Kompres foto bukti (~18 KB) dan ubah ke Base64 agar tersimpan langsung di database tanpa memenuhi folder laptop."""
        try:
            thumb = cv2.resize(snapshot_frame, (480, 360), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 62])
            if not ok:
                return None
            b64 = base64.b64encode(buf.tobytes()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception:
            return None

    # ---- simpan otomatis ke Log Kejadian (langsung ke DB) ----
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

                snap_data_uri = self._encode_snapshot_for_db(snapshot_frame)

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
                    status=STATUS_FLOW[0],
                    snapshot=snap_data_uri,
                    note=note_text,
                )
                db.add(ev)
                db.flush()

                # Batasi penyimpanan foto maksimal pada 80 log terbaru agar ukuran arina.db tetap ringan (< 2 MB)
                old_events = (
                    db.query(Event)
                    .filter(Event.snapshot.isnot(None))
                    .order_by(Event.id.desc())
                    .offset(80)
                    .all()
                )
                for old_ev in old_events:
                    old_ev.snapshot = None

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
                print(f"[ARINA] Tersimpan langsung ke DB: {category} ({confidence:.1f}%) | blur_orang={has_person}")
        except Exception as e:
            print(f"[ARINA] Gagal menyimpan deteksi ke database: {e}")

    # ---- loop ----
    def _loop(self):
        self._open()
        interval = 1.0 / getattr(config, "TARGET_FPS", 15)
        idle_timeout = getattr(config, "IDLE_TIMEOUT", 60)
        jpeg_quality = getattr(config, "JPEG_QUALITY", 75)
        try:
            while self._running and not self._stopped_by_user and time.time() - self._last_touch < idle_timeout:
                t0 = time.time()
                raw_frame, synthetic = self._read(t0)
                if self._stopped_by_user:
                    break

                if self.mode == "yolov8" and (self._coco_model or self._custom_model) and not synthetic:
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