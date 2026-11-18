"""ARINA API - FastAPI. Jalankan: uvicorn main:app --reload --port 8000"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

import config
from auth import create_token, hash_password, require_admin, require_user, verify_password
from categories import CATEGORIES, CATEGORY_NAMES, PLASTIC_NAMES
from database import Base, SessionLocal, engine as db_engine, get_db
from models import Event, MonthlyStat, Point, PointCategoryStat, PointReport, STATUS_FLOW, User
from seed import seed_if_empty
from video import engine as video_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(db_engine)
    with db_engine.begin() as conn:
        try:
            conn.exec_driver_sql("ALTER TABLE point_reports ADD COLUMN photo_data TEXT")
        except Exception:
            pass
    with SessionLocal() as db:
        seed_if_empty(db)
    yield
    video_engine.stop()


app = FastAPI(title="ARINA API - Pemantauan Sungai", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


# ----------------------------------------------------------------- helpers
def point_label(p: Point) -> str:
    return f"{p.code} · {p.zone.lower()}" if p.code in ("T1", "T5") else p.code


def event_out(e: Event, p: Point) -> dict:
    return {
        "id": e.id, "point_code": e.point_code, "point_name": p.name, "zone": p.zone,
        "lat": p.lat, "lng": p.lng, "dms": p.dms,
        "occurred_at": e.occurred_at.isoformat(), "category": e.category, "count": e.count,
        "density": e.density, "confidence": e.confidence, "status": e.status,
        "snapshot_url": f"/static/snapshots/{e.snapshot}" if e.snapshot else None,
        "note": e.note,
    }


def report_out(r: PointReport) -> dict:
    return {
        "id": r.id,
        "reporter_id": r.reporter_id,
        "reporter_name": r.reporter_name,
        "location_name": r.location_name,
        "lat": r.lat,
        "lng": r.lng,
        "density": r.density,
        "description": r.description,
        "photo_data": r.photo_data,
        "status": r.status,
        "created_at": r.created_at.isoformat(),
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "reviewed_by": r.reviewed_by,
        "review_note": r.review_note,
    }


def category_matrix(db: Session) -> dict:
    m = {p.code: {n: 0 for n in CATEGORY_NAMES} for p in db.query(Point).all()}
    for r in db.query(PointCategoryStat).all():
        m[r.point_code][r.category] = r.count
    return m


# ----------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: str
    password: str


class RegisterIn(BaseModel):
    name: str
    email: str
    password: str


class GoogleLoginIn(BaseModel):
    email: str
    name: Optional[str] = None


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email atau password salah.")
    info = {"sub": str(user.id), "email": user.email, "name": user.name, "role": user.role}
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/register")
def register(body: RegisterIn, db: Session = Depends(get_db)):
    clean_email = body.email.strip().lower()
    clean_name = body.name.strip()
    if not clean_name or not clean_email or len(body.password) < 6:
        raise HTTPException(400, "Nama, email, dan sandi minimal 6 karakter wajib diisi.")
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(400, "Email sudah terdaftar. Silakan masuk melalui tab Login.")
    user = User(
        email=clean_email,
        name=clean_name,
        password_hash=hash_password(body.password),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    info = {"sub": str(user.id), "email": user.email, "name": user.name, "role": user.role}
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/google")
def login_google(body: GoogleLoginIn, db: Session = Depends(get_db)):
    clean_email = body.email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(400, "Masukkan alamat email Google yang valid.")
    user = db.query(User).filter(User.email == clean_email).first()
    if not user:
        display_name = (body.name or clean_email.split("@")[0]).strip()
        user = User(
            email=clean_email,
            name=display_name,
            password_hash=hash_password(os.urandom(16).hex()),
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    info = {"sub": str(user.id), "email": user.email, "name": user.name, "role": user.role}
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/guest")
def guest():
    guest_id = f"guest_{os.urandom(4).hex()}"
    info = {"sub": guest_id, "email": "", "name": "Tamu", "role": "guest"}
    return {"token": create_token(info), "user": info}


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    return {"user": {k: user[k] for k in ("sub", "email", "name", "role")}}


# ----------------------------------------------------------------- peta & titik
@app.get("/api/points")
def points(db: Session = Depends(get_db), _: dict = Depends(require_user)):
    matrix = category_matrix(db)
    pts = db.query(Point).order_by(Point.code).all()
    out = []
    for p in pts:
        cats = sorted(matrix[p.code].items(), key=lambda kv: kv[1], reverse=True)
        out.append({
            "code": p.code, "name": p.name, "zone": p.zone, "label": point_label(p),
            "lat": p.lat, "lng": p.lng, "dms": p.dms, "density": p.density,
            "total": sum(matrix[p.code].values()),
            "dominant": [{"category": c, "count": n} for c, n in cats[:3] if n > 0],
        })
    week_ago = datetime.now() - timedelta(days=7)
    events = db.query(Event).all()
    return {
        "center": [-6.0355, 106.1631], "zoom": 15, "points": out,
        "summary": {
            "points": len(pts),
            "events_this_week": sum(1 for e in events if e.occurred_at >= week_ago),
            "pending": sum(1 for e in events if e.status == STATUS_FLOW[0]),
        },
    }


# ----------------------------------------------------------------- penitikan koordinat masyarakat & validasi petugas
class PointReportIn(BaseModel):
    location_name: str
    lat: float
    lng: float
    density: str = "Sedang"
    description: Optional[str] = None
    photo_data: Optional[str] = None


class PointReportReviewIn(BaseModel):
    status: str  # "Valid" | "Ditolak"
    density: Optional[str] = None
    review_note: Optional[str] = None
    # Data cadangan agar validasi tetap sukses meski kontainer serverless baru menyala
    location_name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    description: Optional[str] = None
    reporter_id: Optional[str] = None
    reporter_name: Optional[str] = None
    created_at: Optional[str] = None
    photo_data: Optional[str] = None


@app.get("/api/point-reports")
def list_point_reports(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    rows = db.query(PointReport).order_by(PointReport.created_at.desc()).all()
    if user.get("role") == "admin":
        visible = rows
    else:
        uid = str(user.get("sub"))
        visible = [r for r in rows if r.status == "Valid" or r.reporter_id == uid]
    return {"reports": [report_out(r) for r in visible]}


@app.post("/api/point-reports")
def create_point_report(body: PointReportIn, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    loc = body.location_name.strip()
    if not loc:
        raise HTTPException(400, "Nama lokasi atau patokan sekitar sungai wajib diisi.")
    if not (-90 <= body.lat <= 90 and -180 <= body.lng <= 180):
        raise HTTPException(400, "Koordinat latitude/longitude tidak valid.")
    if body.photo_data and len(body.photo_data) > 2_000_000:
        raise HTTPException(400, "Ukuran foto terlalu besar. Silakan pilih foto lain.")
    dens = body.density if body.density in ("Tinggi", "Sedang", "Rendah") else "Sedang"

    rep = PointReport(
        reporter_id=str(user.get("sub")),
        reporter_name=user.get("name") or "Warga",
        location_name=loc,
        lat=round(body.lat, 6),
        lng=round(body.lng, 6),
        density=dens,
        description=(body.description or "").strip() or None,
        photo_data=body.photo_data or None,
        status="Menunggu Peninjauan",
        created_at=datetime.now(),
    )
    db.add(rep)
    db.commit()
    db.refresh(rep)
    return report_out(rep)


@app.patch("/api/point-reports/{report_id}/review")
def review_point_report(report_id: int, body: PointReportReviewIn, db: Session = Depends(get_db),
                        admin: dict = Depends(require_admin)):
    if body.status not in ("Valid", "Ditolak"):
        raise HTTPException(400, "Keputusan peninjauan harus 'Valid' atau 'Ditolak'.")

    rep = db.get(PointReport, report_id)
    if not rep:
        if body.location_name is not None and body.lat is not None and body.lng is not None:
            try:
                created_dt = datetime.fromisoformat(body.created_at) if body.created_at else datetime.now()
            except Exception:
                created_dt = datetime.now()
            rep = PointReport(
                id=report_id,
                reporter_id=body.reporter_id or "warga",
                reporter_name=body.reporter_name or "Warga",
                location_name=body.location_name,
                lat=round(body.lat, 6),
                lng=round(body.lng, 6),
                density=body.density if body.density in ("Tinggi", "Sedang", "Rendah") else "Sedang",
                description=body.description,
                photo_data=body.photo_data,
                status="Menunggu Peninjauan",
                created_at=created_dt,
            )
            db.add(rep)
            db.flush()
        else:
            raise HTTPException(404, "Laporan titik koordinat tidak ditemukan.")

    if body.density in ("Tinggi", "Sedang", "Rendah"):
        rep.density = body.density
    rep.status = body.status
    rep.reviewed_at = datetime.now()
    rep.reviewed_by = admin.get("name") or "Petugas ARINA"
    rep.review_note = (body.review_note or "").strip() or None
    db.commit()
    db.refresh(rep)
    return report_out(rep)


@app.get("/api/categories")
def categories(_: dict = Depends(require_user)):
    return {"categories": CATEGORIES}


# ----------------------------------------------------------------- log kejadian (khusus petugas)
@app.get("/api/events")
def list_events(point: str = "Semua", density: str = "Semua", category: str = "Semua",
                status: str = "Semua", db: Session = Depends(get_db), _: dict = Depends(require_admin)):
    q = db.query(Event)
    if point != "Semua":
        q = q.filter(Event.point_code == point)
    if density != "Semua":
        q = q.filter(Event.density == density)
    if category != "Semua":
        q = q.filter(Event.category == category)
    if status != "Semua":
        q = q.filter(Event.status == status)
    pts = {p.code: p for p in db.query(Point).all()}
    rows = q.order_by(Event.occurred_at.desc()).all()
    per_point = {c: 0 for c in pts}
    for e in db.query(Event).all():
        per_point[e.point_code] += 1
    return {"events": [event_out(e, pts[e.point_code]) for e in rows],
            "total": len(rows), "per_point": per_point, "statuses": STATUS_FLOW}


@app.get("/api/events/{event_id}")
def get_event(event_id: int, db: Session = Depends(get_db), _: dict = Depends(require_admin)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Kejadian tidak ditemukan.")
    return event_out(e, db.get(Point, e.point_code))


class StatusIn(BaseModel):
    status: str


@app.patch("/api/events/{event_id}/status")
def update_status(event_id: int, body: StatusIn, db: Session = Depends(get_db),
                  _: dict = Depends(require_admin)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Kejadian tidak ditemukan.")
    if body.status not in STATUS_FLOW:
        raise HTTPException(400, f"Status harus salah satu dari: {', '.join(STATUS_FLOW)}")
    if STATUS_FLOW.index(body.status) != STATUS_FLOW.index(e.status) + 1:
        raise HTTPException(400, f"Status hanya dapat dinaikkan bertahap: {' → '.join(STATUS_FLOW)}")
    e.status = body.status
    db.commit()
    return event_out(e, db.get(Point, e.point_code))


# ----------------------------------------------------------------- statistik
@app.get("/api/stats")
def stats(point: str = "Semua", db: Session = Depends(get_db), _: dict = Depends(require_user)):
    pts = db.query(Point).order_by(Point.code).all()
    matrix = category_matrix(db)
    selected = [p.code for p in pts] if point == "Semua" else [point]
    if any(c not in matrix for c in selected):
        raise HTTPException(404, "Titik tidak ditemukan.")

    composition = {n: sum(matrix[c][n] for c in selected) for n in CATEGORY_NAMES}
    total = sum(composition.values())
    top_name, top_count = max(composition.items(), key=lambda kv: kv[1])
    plastic = sum(v for k, v in composition.items() if k in PLASTIC_NAMES)

    totals = {p.code: sum(matrix[p.code].values()) for p in pts}
    densest = max(selected, key=lambda c: totals[c])

    ev_q = db.query(Event)
    if point != "Semua":
        ev_q = ev_q.filter(Event.point_code == point)
    evs = ev_q.all()
    resolved = round(100 * sum(1 for e in evs if e.status == STATUS_FLOW[2]) / len(evs)) if evs else 0

    recap = []
    for p in pts:
        cats = matrix[p.code]
        recap.append({
            "code": p.code, "label": point_label(p), "total": totals[p.code],
            "dominant": max(cats.items(), key=lambda kv: kv[1])[0],
            "vs_prev_pct": round((totals[p.code] - p.prev_total) / p.prev_total * 100) if p.prev_total else 0,
        })
    monthly = [{"month": m.label, "plastic": m.plastic, "non_plastic": m.non_plastic,
                "total": m.plastic + m.non_plastic}
               for m in db.query(MonthlyStat).order_by(MonthlyStat.order).all()]
    return {
        "period": "Oktober 2026", "filter": point, "total": total,
        "top_category": {"name": top_name, "count": top_count},
        "densest_point": {"code": densest, "total": totals[densest]},
        "resolved_pct": resolved,
        "plastic_pct": round(100 * plastic / total) if total else 0,
        "by_point": [{"code": p.code, "label": point_label(p), "total": totals[p.code],
                      "categories": matrix[p.code]} for p in pts],
        "composition": [{"category": k, "count": v} for k, v in composition.items()],
        "monthly": monthly, "recap": recap,
    }


# ----------------------------------------------------------------- kamera (khusus petugas)
@app.get("/video_feed")
def video_feed(_: dict = Depends(require_admin)):
    """MJPEG stream. Pakai: <img src="/video_feed?token=..."> (khusus petugas)."""
    return StreamingResponse(video_engine.mjpeg(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/detections/current")
def detections_current(_: dict = Depends(require_admin)):
    video_engine.touch()
    return video_engine.current_detections()


@app.post("/api/camera/stop")
def camera_stop(_: dict = Depends(require_admin)):
    video_engine.stop_camera()
    return {"status": "stopped", **video_engine.status()}


@app.post("/api/camera/start")
def camera_start(_: dict = Depends(require_admin)):
    video_engine.start_camera()
    return {"status": "started", **video_engine.status()}


@app.get("/api/health")
def health():
    st = video_engine.status()
    return {"status": "ok", "use_yolo_model": st["mode"] == "yolov8", **st}
