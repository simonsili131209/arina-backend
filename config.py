"""Konfigurasi utama ARINA."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---- Mode model YOLOv8 -------------------------------------------------------
# False = mode simulasi (bounding box buatan). Setelah best.pt tersedia:
#   1) salin ke backend/models/best.pt
#   2) ubah menjadi True
#   3) pip install ultralytics
USE_YOLO_MODEL = False
MODEL_PATH = BASE_DIR / "models" / "best.pt"
YOLO_CONF = 0.40

# ---- Kamera ------------------------------------------------------------------
CAMERA_INDEX = 0          # 0 = webcam bawaan; ganti dengan URL RTSP/CCTV bila perlu
FRAME_W, FRAME_H = 960, 540
JPEG_QUALITY = 80
TARGET_FPS = 20
IDLE_TIMEOUT = 8          # detik; kamera dilepas bila tidak ada penonton

# ---- Database & file ---------------------------------------------------------
DATABASE_URL = f"sqlite:///{BASE_DIR / 'arina.db'}"
STATIC_DIR = BASE_DIR / "static"
SNAPSHOT_DIR = STATIC_DIR / "snapshots"
STATIC_DIR.mkdir(exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ---- Keamanan ----------------------------------------------------------------
SECRET_KEY = os.getenv("ARINA_SECRET", "ganti-dengan-secret-acak-saat-deploy")
TOKEN_TTL_HOURS = 12
DEMO_ADMIN_EMAIL = "admin@arina.id"
DEMO_ADMIN_PASSWORD = "arina2026"

CORS_ORIGINS = ["*"]
