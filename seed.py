"""Seed data awal dan 4 akun resmi petugas ARINA."""
import random
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from auth import hash_password
from categories import CATEGORIES, CATEGORY_NAMES
from config import SNAPSHOT_DIR
from models import Event, MonthlyStat, Point, PointCategoryStat, STATUS_FLOW, User

OFFICIAL_STAFF = [
    ("petugas1@arina.id", "Petugas 1 · Koordinator", "ArinaPetugas#01"),
    ("petugas2@arina.id", "Petugas 2 · Lapangan", "ArinaPetugas#02"),
    ("admin@arina.id", "Admin Utama ARINA", "ArinaAdmin#2026"),
    ("lapangan@arina.id", "Petugas 4 · Pemantau", "ArinaLapangan#04"),
]

POINTS = [
    ("T1", "Titik 1", "Hulu",         -6.042250, 106.161917, "6°02'32.1\"S 106°09'42.9\"E", "Tinggi"),
    ("T2", "Titik 2", "Tengah Hulu",  -6.038528, 106.163056, "6°02'18.7\"S 106°09'47.0\"E", "Sedang"),
    ("T3", "Titik 3", "Tengah",       -6.035889, 106.163278, "6°02'09.2\"S 106°09'47.8\"E", "Tinggi"),
    ("T4", "Titik 4", "Tengah Hilir", -6.034528, 106.163306, "6°02'04.3\"S 106°09'47.9\"E", "Sedang"),
    ("T5", "Titik 5", "Muara",        -6.026944, 106.164139, "6°01'37.0\"S 106°09'50.9\"E", "Rendah"),
]
PREV_TOTAL = {"T1": 30, "T2": 79, "T3": 64, "T4": 42, "T5": 31}

MATRIX = {
    "T1": [5, 11, 3, 7, 2, 1, 1, 3],
    "T2": [17, 22, 9, 21, 6, 3, 5, 7],
    "T3": [14, 16, 7, 15, 4, 2, 4, 8],
    "T4": [9, 10, 4, 9, 3, 1, 3, 3],
    "T5": [6, 5, 3, 7, 1, 1, 3, 3],
}
MONTHLY = [("Mei", 145, 53), ("Jun", 155, 59), ("Jul", 135, 52),
           ("Agu", 168, 63), ("Sep", 182, 64), ("Okt", 200, 64)]


def density_of(n: int) -> str:
    return "Tinggi" if n >= 12 else "Sedang" if n >= 6 else "Rendah"


def _snapshot(path, point_code, category, count, ts, color_hex, seed):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False
    rng = np.random.default_rng(seed)
    h, w = 360, 640
    grad = np.linspace(0, 1, h - 130)[:, None, None]
    water = (np.array([150, 125, 85]) * (1 - grad) + np.array([120, 100, 62]) * grad).astype(np.uint8)
    img = np.zeros((h, w, 3), np.uint8)
    img[:130] = (96, 100, 84)
    img[130:] = np.repeat(water, w, axis=1)
    cv2.fillPoly(img, [np.array([[0, 130], [90, 130], [0, 260]])], (60, 75, 52))
    cv2.fillPoly(img, [np.array([[w, 130], [w - 110, 130], [w, 280]])], (60, 75, 52))
    c = color_hex.lstrip("#")
    bgr = (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
    for i in range(min(count, 9)):
        x, y = int(rng.integers(150, w - 170)), int(rng.integers(170, h - 60))
        bw, bh = int(rng.integers(24, 52)), int(rng.integers(18, 38))
        cv2.ellipse(img, (x + bw // 2, y + bh // 2), (bw // 2, bh // 2), 0, 0, 360, bgr, -1)
        cv2.rectangle(img, (x - 3, y - 3), (x + bw + 3, y + bh + 3), (80, 200, 80), 2)
    cv2.rectangle(img, (0, h - 34), (w, h), (35, 30, 25), -1)
    cv2.putText(img, f"{point_code} | {category} x{count} | {ts:%d/%m/%Y %H:%M}", (12, h - 11),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(img, "SNAPSHOT SIMULASI", (w - 190, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return True


def seed_if_empty(db: Session) -> None:
    # Pastikan 4 akun resmi petugas selalu tersedia
    for email, name, raw_pw in OFFICIAL_STAFF:
        existing = db.query(User).filter(User.email == email).first()
        if not existing:
            db.add(User(email=email, name=name, password_hash=hash_password(raw_pw), role="admin"))
        else:
            existing.role = "admin"
            existing.password_hash = hash_password(raw_pw)
    db.commit()

    if db.query(Point).count() > 0:
        return

    for code, name, zone, lat, lng, dms, dens in POINTS:
        db.add(Point(code=code, name=name, zone=zone, lat=lat, lng=lng, dms=dms,
                     density=dens, prev_total=PREV_TOTAL[code]))
        for cat, n in zip(CATEGORY_NAMES, MATRIX[code]):
            db.add(PointCategoryStat(point_code=code, category=cat, count=n))

    for i, (label, pl, non) in enumerate(MONTHLY):
        db.add(MonthlyStat(order=i, label=label, plastic=pl, non_plastic=non))

    rng = random.Random(2026)
    plan = ["T1"] * 3 + ["T2"] * 7 + ["T3"] * 5 + ["T4"] * 2 + ["T5"] * 1
    rng.shuffle(plan)
    now = datetime.now().replace(second=0, microsecond=0)
    minutes_ago = sorted(rng.sample(range(25, 7 * 24 * 60), len(plan)))
    color = {c["name"]: c["color"] for c in CATEGORIES}

    for i, (code, ago) in enumerate(zip(plan, minutes_ago)):
        ts = now - timedelta(minutes=ago)
        weights = MATRIX[code]
        category = rng.choices(CATEGORY_NAMES, weights=weights, k=1)[0]
        count = max(2, round(sum(weights) / rng.uniform(6, 9)) + rng.randint(-2, 2))
        status = STATUS_FLOW[0] if i < 4 else STATUS_FLOW[1] if i < 10 else STATUS_FLOW[2]
        ev = Event(point_code=code, occurred_at=ts, category=category, count=count,
                   density=density_of(count), confidence=round(rng.uniform(68, 97), 1),
                   status=status, note="Data contoh hasil simulasi deteksi.")
        db.add(ev)
        db.flush()
        fname = f"evt_{ev.id}.jpg"
        if _snapshot(SNAPSHOT_DIR / fname, code, category, count, ts, color[category], ev.id):
            ev.snapshot = fname
    db.commit()
